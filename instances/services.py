"""Local function-source bookkeeping (see EdgeFunctionSource)."""

from .models import EdgeFunctionSource, Server


def save_function_source(
    server: Server,
    slug: str,
    body: str,
    *,
    name: str = "",
    verify_jwt: bool = True,
    version: int | None = None,
    actor: str = "",
) -> EdgeFunctionSource:
    """Called after every SUCCESSFUL deploy that went through Diabase."""
    obj, _created = EdgeFunctionSource.objects.update_or_create(
        server=server,
        slug=slug,
        defaults={
            "body": body,
            "name": name or slug,
            "verify_jwt": verify_jwt,
            "deployed_version": version,
            "deployed_by": actor,
        },
    )
    return obj


def get_function_source(server: Server, slug: str) -> EdgeFunctionSource | None:
    return EdgeFunctionSource.objects.filter(server=server, slug=slug).first()


def delete_function_source(server: Server, slug: str) -> None:
    EdgeFunctionSource.objects.filter(server=server, slug=slug).delete()
