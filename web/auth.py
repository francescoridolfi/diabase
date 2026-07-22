"""Authentication glue: single-operator login, forced first password change.

The Docker bootstrap seeds one user, admin/admin (see the seed migration).
Whoever logs in with the factory password is flagged in the session and
cannot navigate anywhere except the password-change form until they set
a real one. No public registration: this is a control plane, the operator
decides who gets in (createsuperuser / the Django admin).
"""

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.contrib.auth.views import PasswordChangeView
from django.dispatch import receiver
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy

from audit.services import record

# the well-known bootstrap credential, forced to rotate at first login
FACTORY_PASSWORD = "admin"  # noqa: S105  # nosec B105
SESSION_FLAG = "must_change_password"


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    # one hash check at login, then the session remembers: the middleware
    # below must not pay a password hash on every request
    if user.check_password(FACTORY_PASSWORD):
        request.session[SESSION_FLAG] = True
    record(action="auth.login", actor_type="user", actor=user.get_username())


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    if user is not None:
        record(action="auth.logout", actor_type="user", actor=user.get_username())


class ForcePasswordChangeMiddleware:
    """While the factory password is in place, every page is the
    password-change page (logout stays reachable)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and request.session.get(SESSION_FLAG):
            allowed = (reverse("password_change"), reverse("logout"))
            if request.path not in allowed:
                return redirect("password_change")
        return self.get_response(request)


class ForcedPasswordChangeView(PasswordChangeView):
    template_name = "web/password_change.html"
    success_url = reverse_lazy("home")

    def form_valid(self, form):
        response = super().form_valid(form)
        update_session_auth_hash(self.request, form.user)
        self.request.session.pop(SESSION_FLAG, None)
        record(
            action="auth.password_changed",
            actor_type="user",
            actor=form.user.get_username(),
        )
        return response
