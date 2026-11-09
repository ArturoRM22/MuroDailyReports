from .models import Branch, BranchSettings

def scoped_branches(user):
    """Owner sees every branch; an employee only sees their own."""
    qs = Branch.objects.all()
    if user.role == "employee":
        qs = qs.filter(users=user)
    return qs