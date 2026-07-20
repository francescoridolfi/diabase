from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("servers/create/", views.server_create, name="server_create"),
    path("projects/create/", views.project_create, name="project_create"),
    path("projects/<int:pk>/", views.project_room, name="project_room"),
    path("projects/<int:pk>/turns/start/", views.turn_start, name="turn_start"),
    path("projects/<int:pk>/turns/<int:turn_id>/stream/", views.turn_stream, name="turn_stream"),
    path("projects/<int:pk>/schema/", views.schema_json, name="schema_json"),
    path("projects/<int:pk>/audit/", views.audit_partial, name="audit_partial"),
    path("projects/<int:pk>/audit/log/", views.audit_log, name="audit_log"),
    path("projects/<int:pk>/settings/", views.project_update, name="project_update"),
    path("projects/<int:pk>/files/save/", views.context_file_save, name="context_file_save"),
    path("projects/<int:pk>/files/delete/", views.context_file_delete, name="context_file_delete"),
]
