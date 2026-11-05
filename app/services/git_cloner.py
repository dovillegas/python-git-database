# app/services/git_cloner.py
import base64
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from git import Git, GitCommandError, Repo

from app.database import DATA_DIR

load_dotenv()

REPOS_DIR = DATA_DIR / "repos"


def repo_path(cohorte: int | str, equipo_nombre: str) -> Path:
    """data/repos/{cohorte}/{equipo_nombre}."""
    nombre = equipo_nombre.strip().replace("/", "_").replace(" ", "_")
    return REPOS_DIR / str(cohorte) / nombre


def _git_env(con_token: bool = True) -> dict:
    env = {"GIT_TERMINAL_PROMPT": "0"}
    token = os.getenv("GITHUB_TOKEN")
    if con_token and token:
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {cred}",
        )
    return env


def _remote_head(url: str, env: dict) -> str | None:
    g = Git()
    with g.custom_environment(**env):
        salida = g.ls_remote(url, "HEAD")
    return salida.split()[0] if salida.strip() else None  # None = repo vacío


def sync_repo(url: str, cohorte: int | str, equipo_nombre: str) -> dict:
    """Clona si no existe; si existe, actualiza solo cuando hay commits nuevos."""
    destino = repo_path(cohorte, equipo_nombre)
    env = _git_env(con_token=False) 
    res = {"estado": None, "commit": None, "error": None}
    try:
        remoto = _remote_head(url, env)
        if remoto is None:
            res["estado"] = "vacio"
            return res

        if not (destino / ".git").exists():
            destino.parent.mkdir(parents=True, exist_ok=True)
            Repo.clone_from(url, destino, env=env)
            res["estado"] = "clonado"
        else:
            repo = Repo(destino)
            if repo.head.commit.hexsha == remoto:
                res["estado"] = "sin_cambios"
            else:
                with repo.git.custom_environment(**env):
                    repo.git.fetch("origin", "HEAD")
                    repo.git.reset("--hard", "FETCH_HEAD")
                res["estado"] = "actualizado"

        res["commit"] = Repo(destino).head.commit.hexsha[:7]
    except GitCommandError as e:
        env = _git_env(con_token=True)
        res["estado"] = "error"
        res["error"] = (e.stderr or str(e)).strip()[:300]
    except Exception as e:  # no cortar el lote por un repo
        res["estado"] = "error"
        res["error"] = str(e)[:300]
    return res


def sync_many(items: list[tuple[str, int | str, str]], workers: int = 4, on_result=None) -> dict:
    """items: [(url, cohorte, equipo_nombre)]. Devuelve {equipo_nombre: resultado}."""
    def tarea(item):
        url, cohorte, nombre = item
        r = sync_repo(url, cohorte, nombre)
        if on_result:
            on_result(nombre, r)
        return nombre, r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(tarea, items))