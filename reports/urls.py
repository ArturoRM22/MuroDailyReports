from django.urls import path
from . import views
from rest_framework.authtoken.views import obtain_auth_token

app_name = "reports"

urlpatterns = [
    path("branches/", views.BranchList.as_view(), name="branch-list"),
    path("branches/<int:pk>/", views.BranchDetail.as_view(), name="branch-detail"),
    path("users/", views.UserList.as_view(), name="user-list"),
    path("users/<int:pk>/", views.UserDetail.as_view(), name="user-detail"),
    path("branch_settings/", views.BranchSettingsList.as_view(), name="branch_settings-list"),
    path("reports/", views.ReportList.as_view(), name="daily_reports-list"),
    path("reports/<int:pk>/", views.ReportDetail.as_view(), name="daily_reports-detail"),
    path("api-token-auth/", obtain_auth_token),
]