"""Carga inicial idempotente para rúbricas y parámetros de evaluación."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CriterioEvaluacion, ConfigEvaluacion, Rubrica


_RUBRICAS = (
    ("R1", "Desarrollo del proyecto", 1),
    ("R2", "Aspectos técnicos", 2),
)

_CRITERIOS = (
    ("R1_REPO", "Organización del repositorio", 10.0, 1, "R1"),
    ("R1_NEGOCIO", "Análisis del negocio", 10.0, 2, "R1"),
    ("R1_REGLAS", "Reglas de negocio", 10.0, 3, "R1"),
    ("R1_MODELO", "Modelo de datos", 15.0, 4, "R1"),
    ("R1_FISICA", "Implementación física", 10.0, 5, "R1"),
    ("R1_SQL", "Consultas SQL", 10.0, 6, "R1"),
    ("R1_EVID", "Evidencias", 5.0, 7, "R1"),
    ("R2_PROC", "Procedimientos almacenados", 6.0, 1, "R2"),
    ("R2_TRANS", "Transacciones", 6.0, 2, "R2"),
    ("R2_TRIG", "Triggers", 6.0, 3, "R2"),
    ("R2_SEG", "Seguridad", 6.0, 4, "R2"),
    ("R2_OPT", "Optimización", 6.0, 5, "R2"),
)

GUIAS = {
    "R1_REPO": {
        "que_revisar": "Existencia de README.md\nDescripción general del proyecto\nObjetivo del sistema desarrollado\nGuía para recorrer las carpetas y entregables\nOrganización general del repositorio",
        "evidencia_adecuado": "El README explica el proyecto y permite ubicar las etapas.",
        "evidencia_solido": "Además incluye una explicación clara de la estructura del repositorio.",
        "evidencia_destacado": "La documentación permite comprender rápidamente el proyecto y localizar todos los entregables.",
        "ejemplo_observacion": "README explica claramente la estructura del proyecto.",
    },
    "R1_NEGOCIO": {
        "que_revisar": "Descripción del problema\nAlcance del sistema\nObjetivos del proyecto\nActores involucrados",
        "evidencia_adecuado": "Se entiende claramente qué problema resuelve el sistema.",
        "evidencia_solido": "El alcance y objetivos están correctamente definidos.",
        "evidencia_destacado": "Existe una descripción completa y coherente del dominio de negocio.",
        "ejemplo_observacion": "",
    },
    "R1_REGLAS": {
        "que_revisar": "Identificación de reglas del negocio\nRestricciones operativas\nConsistencia de las reglas",
        "evidencia_adecuado": "Las reglas justifican el modelo de datos.",
        "evidencia_solido": "Son completas y coherentes.",
        "evidencia_destacado": "Cubren adecuadamente los procesos principales del sistema.",
        "ejemplo_observacion": "",
    },
    "R1_MODELO": {
        "que_revisar": "DER\nModelo relacional\nClaves primarias\nClaves foráneas\nNormalización",
        "evidencia_adecuado": "El modelo representa correctamente el negocio.",
        "evidencia_solido": "Relaciones y restricciones correctamente definidas.",
        "evidencia_destacado": "Modelo consistente, normalizado y alineado con los requerimientos.",
        "ejemplo_observacion": "DER completo con cardinalidades correctas.",
    },
    "R1_FISICA": {
        "que_revisar": "Scripts DDL\nRestricciones\nDatos de prueba",
        "evidencia_adecuado": "Los scripts ejecutan correctamente.",
        "evidencia_solido": "Incluyen restricciones e integridad referencial.",
        "evidencia_destacado": "Base implementada completamente y lista para pruebas.",
        "ejemplo_observacion": "",
    },
    "R1_SQL": {
        "que_revisar": "Consultas requeridas\nCorrectitud de resultados\nUso adecuado de SQL",
        "evidencia_adecuado": "Las consultas funcionan correctamente.",
        "evidencia_solido": "Utilizan apropiadamente JOIN, GROUP BY, HAVING u otras técnicas.",
        "evidencia_destacado": "Resuelven necesidades reales del negocio con un diseño adecuado.",
        "ejemplo_observacion": "",
    },
    "R1_EVID": {
        "que_revisar": "Capturas\nResultados\nExplicaciones\nScripts incluidos",
        "evidencia_adecuado": "Existen evidencias suficientes para validar el trabajo.",
        "evidencia_solido": "La demostración es clara y organizada.",
        "evidencia_destacado": "Permite reproducir fácilmente los resultados.",
        "ejemplo_observacion": "",
    },
    "R2_PROC": {
        "que_revisar": "Existencia de procedimientos almacenados\nExistencia de funciones\nUso de parámetros\nAplicación al sistema de ventas",
        "evidencia_adecuado": "Funcionan correctamente.",
        "evidencia_solido": "Resuelven una necesidad real del sistema.",
        "evidencia_destacado": "Se encuentran integrados a procesos importantes del negocio.",
        "ejemplo_observacion": "",
    },
    "R2_TRANS": {
        "que_revisar": "Uso de BEGIN TRANSACTION\nCOMMIT\nROLLBACK\nCasos de éxito y error",
        "evidencia_adecuado": "Se demuestra atomicidad.",
        "evidencia_solido": "Existe validación de errores.",
        "evidencia_destacado": "La solución protege adecuadamente la consistencia de los datos.",
        "ejemplo_observacion": "",
    },
    "R2_TRIG": {
        "que_revisar": "Tabla de auditoría\nTrigger funcional\nRegistro automático de eventos",
        "evidencia_adecuado": "La auditoría funciona.",
        "evidencia_solido": "Registra información útil para trazabilidad.",
        "evidencia_destacado": "Permite reconstruir cambios importantes realizados sobre los datos.",
        "ejemplo_observacion": "Trigger registra cambios de precios en tabla de auditoría.",
    },
    "R2_SEG": {
        "que_revisar": "Usuarios o roles\nPermisos\nRestricciones de acceso",
        "evidencia_adecuado": "Existen mecanismos básicos de control de acceso.",
        "evidencia_solido": "Los permisos responden a distintos perfiles de usuario.",
        "evidencia_destacado": "La estrategia de seguridad está alineada con las necesidades del sistema.",
        "ejemplo_observacion": "",
    },
    "R2_OPT": {
        "que_revisar": "Índices\nConsultas optimizadas\nJustificación de las mejoras",
        "evidencia_adecuado": "Existe alguna optimización verificable.",
        "evidencia_solido": "Se demuestra mejora en consultas relevantes.",
        "evidencia_destacado": "La optimización está correctamente justificada y aplicada sobre consultas reales del proyecto.",
        "ejemplo_observacion": "Se implementó índice sobre PRODUCTO(nombre) y se documentó su uso.",
    },
}

_GUIA_R2 = (
    "Para todos los temas, verificar que se presenten:\n"
    "1. Definición y objetivo.\n"
    "2. Ventajas y aplicaciones.\n"
    "3. Implementación.\n"
    "4. Caso aplicado al sistema.\n"
    "5. Evidencias de funcionamiento.\n\n"
    "La evaluación debe concentrarse principalmente en la implementación y no en la extensión de la investigación."
)

_CONFIGURACION = {
    "umbral_total_pct": "60",
    "umbral_rubrica_pct": "60",
    "escala_max": "5",
    "nivel_0_etiqueta": "No evidencia",
    "nivel_0_descripcion": "El elemento evaluado no existe.",
    "nivel_1_etiqueta": "Insuficiente",
    "nivel_1_descripcion": "Existe evidencia mínima pero incorrecta o incompleta.",
    "nivel_2_etiqueta": "Básico",
    "nivel_2_descripcion": "Cumple parcialmente con lo solicitado.",
    "nivel_3_etiqueta": "Adecuado",
    "nivel_3_descripcion": "Cumple correctamente con los requisitos mínimos.",
    "nivel_4_etiqueta": "Sólido",
    "nivel_4_descripcion": "Cumple completamente con buena calidad técnica.",
    "nivel_5_etiqueta": "Destacado",
    "nivel_5_descripcion": "Supera claramente lo solicitado y presenta evidencias de muy buena calidad.",
}


def sembrar_evaluacion(db: Session) -> None:
    """Inserta rúbricas, criterios y configuración que todavía no existan."""
    try:
        rubricas = {
            rubrica.codigo: rubrica
            for rubrica in db.scalars(select(Rubrica)).all()
        }
        for codigo, nombre, orden in _RUBRICAS:
            if codigo not in rubricas:
                rubrica = Rubrica(codigo=codigo, nombre=nombre, orden=orden)
                db.add(rubrica)
                rubricas[codigo] = rubrica

        if not (rubricas["R2"].guia or "").strip():
            rubricas["R2"].guia = _GUIA_R2

        db.flush()

        criterios_existentes = {
            criterio.codigo: criterio
            for criterio in db.scalars(select(CriterioEvaluacion)).all()
        }
        for codigo, nombre, peso, orden, codigo_rubrica in _CRITERIOS:
            criterio = criterios_existentes.get(codigo)
            if criterio is None:
                criterio = CriterioEvaluacion(
                    rubrica_id=rubricas[codigo_rubrica].id,
                    codigo=codigo,
                    nombre=nombre,
                    peso=peso,
                    orden=orden,
                    activo=True,
                )
                db.add(criterio)
                criterios_existentes[codigo] = criterio
            for campo, valor in GUIAS[codigo].items():
                if not (getattr(criterio, campo) or "").strip():
                    setattr(criterio, campo, valor)

        configuracion_existente = set(
            db.scalars(select(ConfigEvaluacion.clave)).all()
        )
        for clave, valor in _CONFIGURACION.items():
            if clave not in configuracion_existente:
                db.add(ConfigEvaluacion(clave=clave, valor=valor))

        db.commit()
    except Exception:
        db.rollback()
        raise
