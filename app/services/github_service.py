"""GitHub repository health checks and metrics backed by PyGithub."""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

from github import Auth, Github, GithubException
from requests.exceptions import RequestException
from dotenv import load_dotenv
import logging

logging.getLogger("github").setLevel(logging.ERROR)
load_dotenv()

def _github_client() -> Github:
    """Return a PyGithub client, authenticated when ``GITHUB_TOKEN`` is set."""
    token = os.getenv("GITHUB_TOKEN")
    if token:
        return Github(auth=Auth.Token(token))
    return Github()


def _repository_full_name(url: str) -> str:
    """Normalize a GitHub URL (or owner/repo) to ``owner/repo``."""
    value = (url or "").strip().rstrip("/")
    if not value:
        raise ValueError("La URL del repositorio está vacía.")

    if "://" not in value:
        value = f"https://{value}"

    parsed = urlparse(value)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("La URL debe identificar un repositorio como owner/repo.")

    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise ValueError("La URL no contiene un owner y un repositorio válidos.")

    return f"{owner}/{repository}"


def _error_message(error: Exception) -> str:
    if isinstance(error, GithubException):
        message = error.data.get("message") if isinstance(error.data, dict) else None
        return f"GitHub API ({error.status}): {message or str(error)}"
    return str(error)


def healthcheck_repo(url: str, usuarios_oficiales: list[str]) -> dict:
    """Check repository access and compare collaborators/contributors to the roster."""
    result = {
        "accesible": False,
        "colaboradores": [],
        "colaboradores_externos": [],
        "integrantes_sin_contribuciones": [],
        "error": None,
        "fuente_externos": None,
    }
    errors: list[str] = []
    official_users = [
        user.strip().lstrip("@")
        for user in (usuarios_oficiales or [])
        if user and user.strip()
    ]
    official_usernames = {user.lower() for user in official_users}

    try:
        repo = _github_client().get_repo(_repository_full_name(url))
        result["accesible"] = True

        try:
            collaborators = [
                user.login
                for user in repo.get_collaborators(affiliation="all")
                if getattr(user, "login", None)
            ]
            result["colaboradores"] = sorted(set(collaborators), key=str.lower)
            result["colaboradores_externos"] = [
                username
                for username in result["colaboradores"]
                if username.lower() not in official_usernames
            ]
        except (GithubException, RequestException, OSError) as exc:
            errors.append(f"No se pudieron consultar colaboradores: {_error_message(exc)}")

        try:
            contributor_usernames = {
                user.login
                for user in repo.get_contributors()
                if getattr(user, "login", None)
            }
            contributor_lower = {u.lower() for u in contributor_usernames}
            result["integrantes_sin_contribuciones"] = [
                username
                for username in official_users
                if username.lower() not in contributor_lower
            ]
            # Si no hubo acceso a colaboradores, detectar externos por contribuciones
            if not result["colaboradores"]:
                result["colaboradores_externos"] = sorted(
                    (u for u in contributor_usernames
                     if u.lower() not in official_usernames),
                    key=str.lower,
                )
                result["fuente_externos"] = "contribuyentes"
            else:
                result["fuente_externos"] = "colaboradores"
        except (GithubException, RequestException, OSError) as exc:
            errors.append(f"No se pudieron consultar contribuciones: {_error_message(exc)}")
    except (GithubException, RequestException, OSError, ValueError) as exc:
        errors.append(_error_message(exc))

    if errors:
        result["error"] = "; ".join(errors)
    return result


def metricas_repo(url: str) -> dict:
    """Return total commits, open issues (excluding PRs), and open PRs."""
    result = {
        "commits": None,
        "issues_abiertos": None,
        "prs_abiertos": None,
        "error": None,
    }

    try:
        repo = _github_client().get_repo(_repository_full_name(url))
        result["commits"] = repo.get_commits().totalCount
        # GitHub's issues endpoint also returns pull requests, so filter them out.
        result["issues_abiertos"] = sum(
            1
            for issue in repo.get_issues(state="open")
            if issue.pull_request is None
        )
        result["prs_abiertos"] = repo.get_pulls(state="open").totalCount
    except (GithubException, RequestException, OSError, ValueError) as exc:
        result["error"] = _error_message(exc)

    return result


def metricas_estudiantes(url: str) -> dict:
    """Return contributor metrics keyed by GitHub username using repository stats."""
    result: dict = {}

    try:
        repo = _github_client().get_repo(_repository_full_name(url))
        stats = None
        # GitHub may asynchronously calculate these stats after its first 202 response.
        for attempt in range(3):
            stats = repo.get_stats_contributors()
            if stats is not None:
                break
            if attempt < 2:
                time.sleep(1)

        if stats is None:
            result["error"] = (
                "Las estadísticas de contribuidores todavía no están disponibles."
            )
            return result

        for contributor in stats:
            author = getattr(contributor, "author", None)
            username = getattr(author, "login", None)
            if not username:
                continue

            weeks = getattr(contributor, "weeks", None) or []
            result[username] = {
                "commits": getattr(contributor, "total", 0) or 0,
                "lineas_agregadas": sum(
                    getattr(week, "a", getattr(week, "additions", 0)) or 0
                    for week in weeks
                ),
                "lineas_eliminadas": sum(
                    getattr(week, "d", getattr(week, "deletions", 0)) or 0
                    for week in weeks
                ),
            }
    except (GithubException, RequestException, OSError, ValueError) as exc:
        result["error"] = _error_message(exc)

    return result