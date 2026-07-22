from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from .auth import ForcedPasswordChangeView

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="web/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/", ForcedPasswordChangeView.as_view(), name="password_change"),
    path("", views.home, name="home"),
    path("connections/", views.connections, name="connections"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/connections/create/", views.connection_create, name="connection_create"),
    path("settings/connections/<int:pk>/delete/", views.connection_delete, name="connection_delete"),
    path("servers/create/", views.server_create, name="server_create"),
    path("servers/<int:pk>/delete/", views.server_delete, name="server_delete"),
    path("projects/create/", views.project_create, name="project_create"),
    path("projects/<int:pk>/delete/", views.project_delete, name="project_delete"),
    path("projects/<int:pk>/", views.project_room, name="project_room"),
    path("projects/<int:pk>/chats/create/", views.chat_create, name="chat_create"),
    path("projects/<int:pk>/chats/<int:chat_id>/delete/", views.chat_delete, name="chat_delete"),
    path("projects/<int:pk>/turns/start/", views.turn_start, name="turn_start"),
    path("projects/<int:pk>/turns/<int:turn_id>/stream/", views.turn_stream, name="turn_stream"),
    path("projects/<int:pk>/plans/<int:plan_id>/", views.plan_json, name="plan_json"),
    path("projects/<int:pk>/plans/<int:plan_id>/approve/", views.plan_approve, name="plan_approve"),
    path("projects/<int:pk>/plans/<int:plan_id>/reject/", views.plan_reject, name="plan_reject"),
    path("projects/<int:pk>/plans/<int:plan_id>/revise/", views.plan_revise, name="plan_revise"),
    path("projects/<int:pk>/schema/", views.schema_json, name="schema_json"),
    path("projects/<int:pk>/functions/", views.functions_json, name="functions_json"),
    path(
        "projects/<int:pk>/functions/<slug:slug>/source/",
        views.function_source_json,
        name="function_source_json",
    ),
    path(
        "projects/<int:pk>/functions/<slug:slug>/deploy/",
        views.function_deploy,
        name="function_deploy",
    ),
    path("projects/<int:pk>/storage/", views.storage_json, name="storage_json"),
    path("projects/<int:pk>/audit/", views.audit_partial, name="audit_partial"),
    path("projects/<int:pk>/audit/log/", views.audit_log, name="audit_log"),
    path("projects/<int:pk>/settings/", views.project_update, name="project_update"),
    path("projects/<int:pk>/files/save/", views.context_file_save, name="context_file_save"),
    path("projects/<int:pk>/files/get/", views.context_file_json, name="context_file_json"),
    path("projects/<int:pk>/files/delete/", views.context_file_delete, name="context_file_delete"),
]
