from django.contrib.auth import update_session_auth_hash
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Branch, User, BranchSettings, DailyReport
from .permissions import scoped_branches
from django.utils.dateparse import parse_date
from .serializers import BranchSerializer, SelfSerializer, UserSerializer, BranchSettingsSerializer, DailyReportSerializer

# Clients naturally send back what a GET returned. Tolerate these.
ROUND_TRIP_FIELDS = {"id", "date_joined", "last_login"}


def serializer_class_for(request):
    """Owners see the full record; employees only their own, narrowed."""
    return UserSerializer if request.user.role == "owner" else SelfSerializer


class BranchList(APIView):
    def get(self, request):
        qs = scoped_branches(request.user)
        return Response(BranchSerializer(qs, many=True).data)

    def post(self, request):
        if request.user.role != "owner":
            return Response({"detail": "Solo el dueño puede crear sucursales."},
                            status=status.HTTP_403_FORBIDDEN)
        ser = BranchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)      # ← without this, bad input = 500
        obj = ser.save()
        return Response(BranchSerializer(obj).data, status=status.HTTP_201_CREATED)


class BranchDetail(APIView):
    def get(self, request, pk):
        obj = get_object_or_404(scoped_branches(request.user), pk=pk)
        return Response(BranchSerializer(obj).data)

    def delete(self, request, pk):
        if request.user.role != "owner":
            return Response({"detail": "Solo el dueño puede editar sucursales."},
                            status=status.HTTP_403_FORBIDDEN)
        obj = get_object_or_404(Branch.objects.all(), pk=pk)
        obj.is_active = False                  # soft delete, see below
        obj.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserList(APIView):
    def get(self, request):
        if request.user.role != "owner":
            return Response({"detail": "Solo el dueño puede consultar usuarios"},
                           status=status.HTTP_403_FORBIDDEN)
        users = User.objects.all()
        return Response(UserSerializer(users, many=True).data)


class UserDetail(APIView):
    def get(self, request, pk):
        # Checked before the lookup: a 403 here must not confirm that the id exists.
        if request.user.role != "owner" and pk != request.user.pk:
            return Response({"detail": "Solo puedes consultar tu propio usuario."},
                            status=status.HTTP_403_FORBIDDEN)
        obj = get_object_or_404(User.objects.all(), pk=pk)
        return Response(serializer_class_for(request)(obj).data)

    def patch(self, request, pk):
        # Same gate as get(), run BEFORE the lookup.
        if request.user.role != "owner" and pk != request.user.pk:
            return Response({"detail": "Solo puedes editar tu propio usuario."},
                            status=status.HTTP_403_FORBIDDEN)

        obj = get_object_or_404(User.objects.all(), pk=pk)
        ser = serializer_class_for(request)(
            obj, data=request.data, partial=True, context={"request": request}
        )

        # DRF would silently DROP anything not writable -- including a
        # privilege-escalation attempt, which would come back as a 200.
        # Reject explicitly so the attempt is visible.
        writable = {n for n, f in ser.fields.items() if not f.read_only}
        keys = set(request.data) if isinstance(request.data, dict) else set()
        rejected = keys - writable - ROUND_TRIP_FIELDS
        if rejected:
            return Response(
                {"detail": "No tienes permiso para modificar: "
                           + ", ".join(sorted(rejected))},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser.is_valid(raise_exception=True)
        ser.save()

        # Without this, changing your OWN password logs you out on your very
        # next request -- Django stores a hash of it in the session.
        if request.user.pk == obj.pk:
            update_session_auth_hash(request, obj)

        return Response(ser.data)      # 200, not 201

class BranchSettingsList(APIView):
    def get(self, request):
        qs = BranchSettings.objects.filter(branch__in=scoped_branches(request.user))
        return Response(BranchSettingsSerializer(qs, many=True).data)

    def post(self, request):
        if request.user.role != "owner":
            return Response({"detail": "No tienes permisos para crear configuraciones."}, status=status.HTTP_403_FORBIDDEN)
        ser = BranchSettingsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = ser.save()
        return Response(BranchSettingsSerializer(obj).data, status=status.HTTP_201_CREATED)

class ReportList(APIView):
    def get(self, request):
        qs = DailyReport.objects.filter(branch__in=scoped_branches(request.user))

        # ?branch=<id> — only your own scope, otherwise don't leak existence
        branch_id = request.query_params.get("branch")
        if branch_id is not None:
            if not scoped_branches(request.user).filter(pk=branch_id).exists():
                return Response({"detail": "No tienes acceso a esta sucursal."},
                                status=status.HTTP_403_FORBIDDEN)
            qs = qs.filter(branch_id=branch_id)

        # ?date=YYYY-MM-DD
        date_str = request.query_params.get("date")
        if date_str is not None:
            d = parse_date(date_str)
            if d is None:
                return Response({"detail": "Formato de fecha inválido. Use AAAA-MM-DD."},
                                status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(report_date=d)

        return Response(DailyReportSerializer(qs, many=True,
                                              context={"request": request}).data)

    def post(self, request):
        data = request.data

        if request.user.role == "employee":
            my_branch = request.user.branch
            if my_branch is None:
                return Response({"detail": "No tienes sucursal asignada."},
                                status=status.HTTP_403_FORBIDDEN)
            sent = request.data.get("branch")
            if sent is not None and sent != my_branch.pk:
                return Response({"detail": "Solo puedes crear reportes de tu sucursal."},
                                status=status.HTTP_403_FORBIDDEN)
            # Branch stays writable in the serializer so owners can choose it,
            # but here we force it BEFORE validation...
            data = request.data.copy()
            data["branch"] = my_branch.pk

        if "branch" not in data:
            return Response({"detail": "La sucursal es obligatoria."},
                            status=status.HTTP_400_BAD_REQUEST)

        ser = DailyReportSerializer(data=data, context={"request": request})
        ser.is_valid(raise_exception=True)

        # ...which is what lets the (branch, report_date) check happen with the
        # real branch. Only relevant for employees: with branch injected via
        # save(), DRF's validator would never see it.
        dup = DailyReport.objects.filter(
            branch=ser.validated_data["branch"],
            report_date=ser.validated_data["report_date"],
        ).exists()
        if dup:
            return Response({"detail": "Ya existe un reporte para esta sucursal en esta fecha."},
                            status=status.HTTP_400_BAD_REQUEST)

        obj = ser.save(created_by=request.user)
        return Response(DailyReportSerializer(obj, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)
        
