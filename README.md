# Seguimiento de Proyectos BD1

Aplicación web para gestionar cohortes, comisiones y equipos de estudiantes de la
cátedra de Bases de Datos I (UNNE), sincronizar repositorios de GitHub, calcular
indicadores de actividad y registrar evaluaciones mediante rúbricas.

> **Uso local.** La aplicación no tiene autenticación y puede almacenar datos
> personales (DNI y correo electrónico). No exponerla en una red abierta sin
> incorporar control de acceso.

## Funcionalidades

<!-- BEGIN:modulos -->
- **Importación:** cargar planillas CSV, XLSX y XLS con cohortes, comisiones,
  equipos y estudiantes.
- **Descargas:** clonar o actualizar repositorios de un equipo o de varios equipos,
  con filtros por cohorte y comisión y seguimiento del progreso.
- **Sincronización y métricas:** descargar repositorios, calcular indicadores desde
  el historial Git local y consultar el estado del proceso.
- **Panel de indicadores:** consultar resúmenes, equipos con alertas y detalles de
  actividad por equipo e integrante.
- **Equipos y estudiantes:** crear y editar equipos, administrar integrantes y
  consultar sus fichas.
- **Autores Git:** asignar autores detectados a estudiantes, desasignarlos o
  ignorarlos; recalcular las métricas afectadas.
- **Evaluación:** configurar rúbricas, criterios, pesos y umbrales; valorar equipos
  con niveles y observaciones; importar y exportar evaluaciones CSV.
- **Mantenimiento:** consultar el uso de datos, descargar respaldos y borrar
  métricas, datos o repositorios, con confirmación.
<!-- END:modulos -->

## Requisitos

| Componente | Versión | Para qué |
|---|---|---|
| Python | 3.11 o superior | Ejecutar la aplicación |
| Git | Instalación disponible en el sistema | Clonar y actualizar repositorios |
| Token de GitHub | Opcional | Acceder a repositorios privados y ampliar el límite de consultas a la API |

## Instalación en Linux (Debian / Ubuntu / Linux Mint)

```bash
# 1. Prerrequisitos
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version
git --version

# 2. Obtener el código
git clone <URL_DEL_REPOSITORIO> bd1-seguimiento
cd bd1-seguimiento

# 3. Crear y activar el entorno virtual
python3 -m venv .venv
source .venv/bin/activate

# 4. Instalar dependencias
pip install --upgrade pip
pip install -r requirements.txt

# 5. Crear la configuración local
cp .env.example .env
nano .env

# 6. Ejecutar
uvicorn app.main:app --reload
```

Abrir <http://127.0.0.1:8000>.

Si la versión disponible de Python es anterior a 3.11, instalar Python 3.11 y su
módulo `venv` desde los repositorios de la distribución y crear el entorno con
`python3.11 -m venv .venv`.

## Instalación en Windows 10 / 11

1. **Instalar Python 3.11 o superior** desde <https://www.python.org/downloads/>.
   Marcar **"Add python.exe to PATH"** durante la instalación.
2. **Instalar Git for Windows** desde <https://git-scm.com/download/win>. En la
   pantalla *Adjusting your PATH environment* elegir **"Git from the command line
   and also from 3rd-party software"**.
3. **Verificar la instalación** en una nueva terminal de PowerShell:

   ```powershell
   python --version
   git --version
   ```

   Si alguno de los comandos no se reconoce, agregar su carpeta al `Path` y abrir
   otra terminal.
4. **Habilitar rutas largas** para los repositorios clonados. En PowerShell como
   administrador:

   ```powershell
   git config --system core.longpaths true
   ```

5. **Instalar y ejecutar la aplicación:**

   ```powershell
   git clone <URL_DEL_REPOSITORIO> bd1-seguimiento
   cd bd1-seguimiento

   py -3 -m venv .venv
   .venv\Scripts\Activate.ps1

   python -m pip install --upgrade pip
   pip install -r requirements.txt

   copy .env.example .env
   notepad .env

   uvicorn app.main:app --reload
   ```

   Si PowerShell bloquea la activación del entorno, permitir scripts locales para
   el usuario actual:

   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```

   En `cmd.exe`, activar el entorno con `.venv\Scripts\activate.bat`.

Abrir <http://127.0.0.1:8000>.

## Configuración (.env)

El archivo `.env` se guarda en la raíz del proyecto y no se debe subir a Git. Usar
`.env.example` como base:

| Variable | Obligatoria | Descripción |
|---|---|---|
| `GITHUB_TOKEN` | No | Token para consultas autenticadas a GitHub y acceso a los repositorios privados autorizados. Sin token, la API pública aplica su límite anónimo. |
| `DATA_DIR` | No | Directorio de datos. Por defecto, `data/` en la raíz del proyecto. |

Para utilizar repositorios privados, crear un token de GitHub con los permisos
necesarios para los repositorios que se van a consultar. No compartir el token ni
incluirlo en el código.

## Primer uso

1. **Importar** una planilla desde el menú *Importar*. Columnas reconocidas:
   `cohorte, comision, equipo, proyecto_nombre, url_github, apellido_nombre, dni,
   email, usuario_github`.
2. **Sincronizar** los repositorios desde *Sincronización* o *Descargas*. Se puede
   filtrar por cohorte y comisión, y procesar un equipo individualmente.
3. **Calcular** o ejecutar la sincronización con cálculo para generar indicadores a
   partir de los repositorios locales.
4. **Revisar Autores Git** y asignar, desasignar o ignorar las identidades que no se
   asociaron automáticamente.
5. **Consultar Equipos** y registrar las valoraciones desde *Evaluación*.

Después de descargar cambios nuevos, volver a calcular los indicadores.

## Estructura de directorios

<!-- BEGIN:estructura -->
```
bd1-seguimiento/
├── app/
│   ├── main.py            # Inicialización FastAPI y registro de routers
│   ├── database.py        # Conexión SQLite, sesión y configuración de DATA_DIR
│   ├── migrations.py      # Migraciones de esquema existentes
│   ├── models.py          # Modelos SQLAlchemy
│   ├── routers/           # Rutas web por módulo
│   ├── services/          # Importación, GitHub, métricas, evaluación y mantenimiento
│   └── templates/         # Plantillas Jinja2 con interacciones HTMX
├── data/                  # Base de datos, planillas y datos de ejecución
│   ├── app.db             # Base de datos SQLite
│   ├── repos/             # Repositorios: repos/{cohorte}/{equipo}/
│   ├── rubrica/           # Plantillas y material de rúbricas
│   ├── uploads/           # Archivos importados y previsualizaciones
│   └── backups/           # Se crea al generar respaldos
├── requirements.txt
├── .env.example
├── .env                   # Configuración local, no se debe subir a Git
├── .gitignore
└── README.md
```
<!-- END:estructura -->

La aplicación crea `data/` al inicializarse y crea `uploads/` o `backups/` cuando
se necesitan. Mantener los datos de ejecución fuera del código fuente.

## Módulos y rutas

<!-- BEGIN:rutas -->
| Módulo | Rutas principales | Funcionalidad |
|---|---|---|
| Panel y equipos | `/`, `/equipos`, `/equipos/nuevo`, `/equipos/{equipo_id}`, `/estudiantes/{estudiante_id}` | Dashboard, filtros, alta y edición de equipos, administración y consulta de integrantes, métricas y acciones de recálculo. |
| Autores Git | `/autores`, `/autores/{autor_id}/asignar`, `/autores/{autor_id}/ignorar`, `/autores/{autor_id}/desasignar`, `/estudiantes/{estudiante_id}/editar` | Revisar identidades Git, asociarlas a estudiantes y editar o quitar integrantes. |
| Descargas | `/descargas`, `/descargas/equipo/{equipo_id}`, `/descargas/lote`, `/descargas/progreso` | Clonar o actualizar un repositorio o un lote y consultar el progreso. |
| Sincronización | `/sincronizacion`, `/sincronizacion/ejecutar`, `/sincronizacion/equipo/{equipo_id}`, `/sincronizacion/progreso` | Descargar, calcular métricas o realizar ambas acciones; consultar el estado. |
| Importación y mantenimiento | `/importar`, `/mantenimiento/metricas`, `/mantenimiento/datos`, `/mantenimiento/repos`, `/mantenimiento/todo`, `/mantenimiento/respaldo` | Importar planillas, administrar datos y repositorios, y descargar respaldos. |
| Evaluación | `/evaluacion`, `/evaluacion/rubricas`, `/evaluacion/equipo/{equipo_id}`, `/evaluacion/importar`, `/evaluacion/plantilla.csv`, `/evaluacion/exportar.csv` | Consultar resultados, administrar rúbricas, valorar equipos e importar o exportar CSV. |
<!-- END:rutas -->

## Dependencias

<!-- BEGIN:dependencias -->
FastAPI y Uvicorn (aplicación y servidor web), SQLAlchemy (persistencia SQLite),
Pandas, OpenPyXL y xlrd (procesamiento de planillas), PyGithub y httpx (integración
con GitHub), GitPython (operaciones Git), Jinja2 y python-multipart (plantillas y
formularios), python-dotenv (lectura de `.env`) y requests (solicitudes HTTP).
<!-- END:dependencias -->

## Mover la aplicación a otra máquina

La base de datos guarda las URL de los repositorios y no rutas locales, por lo que
se puede trasladar `data/` entre sistemas operativos.

| Elemento | Cómo | Notas |
|---|---|---|
| Código | Clonar el repositorio en la máquina nueva | No copiar `.venv/`; se debe crear de nuevo. |
| `.env` | Copiar de forma segura | Contiene el token, si está configurado. No enviarlo por correo o chat. |
| `data/app.db` | Copiar con la aplicación detenida | Contiene los equipos, métricas, autores y evaluaciones. |
| `data/uploads/` | Copiar si se necesitan los archivos importados | Puede no existir si no se han realizado importaciones. |
| `data/repos/` | Copiar o volver a descargar | Puede ocupar bastante espacio. |
| `data/backups/` | Copiar si se necesitan los respaldos | Se crea cuando se genera el primer respaldo. |

**Procedimiento:**

1. Detener la aplicación (`Ctrl+C`) y esperar a que finalice cualquier proceso de
   sincronización.
2. Instalar el código y sus dependencias en la máquina nueva, sin iniciar todavía
   la aplicación.
3. Copiar `.env` y los datos necesarios. Por ejemplo, desde Linux:

   ```bash
   rsync -av --progress data/ usuario@destino:~/bd1-seguimiento/data/
   scp .env usuario@destino:~/bd1-seguimiento/.env
   ```

   En Windows, usar PowerShell:

   ```powershell
   robocopy D:\origen\data D:\destino\data /E
   copy D:\origen\.env D:\destino\.env
   ```

4. Iniciar la aplicación y verificar los datos desde el panel.
5. Si no se copió `data/repos/`, abrir *Sincronización* y volver a descargar los
   repositorios; después, calcular los indicadores.

No ejecutar simultáneamente varias copias de la aplicación con bases de datos
distintas esperando que se fusionen. Al definir `DATA_DIR`, utilizar una ruta válida
en el sistema destino; si no se define, se utiliza `data/` en el proyecto.

## Respaldo

Desde *Importar → Mantenimiento* se puede descargar un respaldo de la base. Para
restaurarlo, detener la aplicación y reemplazar `data/app.db` por el archivo de
respaldo.

## Actualizar la aplicación

```bash
git pull
pip install -r requirements.txt
```

Antes de actualizar, respaldar los datos importantes. Al iniciar, la aplicación
ejecuta su inicialización de base de datos y la migración de esquema implementada.

## Solución de problemas

| Síntoma | Causa y solución |
|---|---|
| `git` no se reconoce como comando (Windows) | Git no está en el `Path`. Revisar la instalación y abrir una terminal nueva. |
| Error de autenticación al clonar | Comprobar que el token sea válido y tenga acceso al repositorio privado. Para repositorios públicos, probar sin token. |
| La API de GitHub limita las consultas | Configurar `GITHUB_TOKEN` en `.env` y reiniciar la aplicación. |
| `ModuleNotFoundError` | Activar `.venv` e instalar de nuevo las dependencias con `pip install -r requirements.txt`. |
| Error de columna inexistente al iniciar | Respaldar `data/app.db` y revisar la migración de esquema correspondiente antes de modificar la base. |
| El puerto 8000 está ocupado | Iniciar con otro puerto, por ejemplo `uvicorn app.main:app --port 8001`. |