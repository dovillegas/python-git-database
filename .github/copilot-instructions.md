
# Contexto del Proyecto: Sistema de Seguimiento de Proyectos BD1 (UNNE)

## Objetivo
Desarrollar una aplicación Web en Python (FastAPI + HTMX/Jinja2 + SQLite) para la cátedra de Bases de Datos I (UNNE).
Permitirá gestionar cohortes (2026, 2027...), comisiones y equipos de estudiantes, analizando sus repositorios de GitHub y automatizando la descarga masiva local.

## Stack Tecnológico
- **Backend:** Python 3.11, FastAPI, SQLAlchemy, SQLite.
- **Frontend:** Jinja2 Templates, HTMX, Tailwind CSS (vía CDN o CLI).
- **Integración GitHub:** PyGithub, GitPython, httpx.
- **Procesamiento de datos:** Pandas, OpenPyXL (importación CSV/Excel).
- **Entorno:** Python venv en VS Code.

## Reglas de Arquitectura e Interfaces
1. **Navegación bidireccional:** Todo estudiante debe enlazar a su equipo y todo equipo a sus estudiantes.
2. **Descarga masiva:** Los repositorios se clonan/actualizan en `data/repos/{cohorte}/{equipo}` (dentro del proyecto).
3. **Métricas de GitHub:**
   - Salud del repo (accesibilidad, colaboradores asignados vs. usuarios externos detectados).
   - Indicadores por Equipo y por Estudiante (Commits, Modificaciones +/-, Issues, PRs, Evolución de archivos).

## Estructura de Base de Datos Solicitada
- `Cohorte` (id, año)
- `Comision` (id, cohorte_id, numero)
- `Equipo` (id, comision_id, nombre_proyecto, url_github, estado_salud)
- `Estudiante` (id, equipo_id, apellido_nombre, dni, email, usuario_github)
- `MetricaProyecto` (id, equipo_id, fecha, total_commits, issues_abiertos, etc.)
- `MetricaEstudiante` (id, estudiante_id, fecha, commits_count, lineas_agregadas, lineas_eliminadas)

## Reglas de Entorno y Agente
1. Utilizar entornos virtuales (`.venv` o `venv`). Las dependencias se gestionan globalmente mediante `requirements.txt`.
2. La estructura del proyecto DEBE mantenerse de la siguiente manera:
   - `app/`: Contiene el código fuente de FastAPI (modelos, servicios, endpoints, plantillas).
   - `data/`: Reservado EXCLUSIVAMENTE para archivos descargados (repositorios git, CSVs, base de datos SQLite).
3. La ruta de datos se define una sola vez en `app/database.py` (`DATA_DIR`, por defecto `./data`, sobrescribible con la variable de entorno `DATA_DIR`). Los demás módulos deben importarla desde ahí.