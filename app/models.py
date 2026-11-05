from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Cohorte(Base):
    __tablename__ = "cohortes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    anio: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)

    comisiones: Mapped[list[Comision]] = relationship(back_populates="cohorte")


class Comision(Base):
    __tablename__ = "comisiones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cohorte_id: Mapped[int] = mapped_column(ForeignKey("cohortes.id"), nullable=False)
    numero: Mapped[int] = mapped_column(Integer, nullable=False)

    cohorte: Mapped[Cohorte] = relationship(back_populates="comisiones")
    equipos: Mapped[list[Equipo]] = relationship(back_populates="comision")


class Equipo(Base):
    __tablename__ = "equipos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comision_id: Mapped[int] = mapped_column(ForeignKey("comisiones.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    nombre_proyecto: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url_github: Mapped[str] = mapped_column(String(2048), nullable=False)
    estado_salud: Mapped[str | None] = mapped_column(String(100), nullable=True)

    comision: Mapped[Comision] = relationship(back_populates="equipos")
    estudiantes: Mapped[list[Estudiante]] = relationship(back_populates="equipo")
    metricas: Mapped[list[MetricaProyecto]] = relationship(back_populates="equipo")
    autores_git: Mapped[list[AutorGit]] = relationship(back_populates="equipo")
    evaluacion: Mapped[Evaluacion | None] = relationship(
        back_populates="equipo", cascade="all, delete-orphan"
    )


class Estudiante(Base):
    __tablename__ = "estudiantes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipo_id: Mapped[int] = mapped_column(ForeignKey("equipos.id"), nullable=False)
    apellido_nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    dni: Mapped[str] = mapped_column(String(32), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    usuario_github: Mapped[str | None] = mapped_column(String(100), nullable=True)

    equipo: Mapped[Equipo] = relationship(back_populates="estudiantes")
    metricas: Mapped[list[MetricaEstudiante]] = relationship(back_populates="estudiante")
    autores_git: Mapped[list[AutorGit]] = relationship(back_populates="estudiante")


class MetricaProyecto(Base):
    __tablename__ = "metricas_proyecto"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipo_id: Mapped[int] = mapped_column(ForeignKey("equipos.id"), nullable=False)
    fecha: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    total_commits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issues_abiertos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prs_abiertos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ultimo_commit: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dias_sin_actividad: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lineas_agregadas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lineas_eliminadas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archivos_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prs_mergeados: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issues_cerrados: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detalle_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    equipo: Mapped[Equipo] = relationship(back_populates="metricas")


class MetricaEstudiante(Base):
    __tablename__ = "metricas_estudiante"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    estudiante_id: Mapped[int] = mapped_column(
        ForeignKey("estudiantes.id"), nullable=False
    )
    fecha: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    commits_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lineas_agregadas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lineas_eliminadas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archivos_tocados: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dias_activos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    primer_commit: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ultimo_commit: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    prs_abiertos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviews: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detalle_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    estudiante: Mapped[Estudiante] = relationship(back_populates="metricas")


class AutorGit(Base):
    __tablename__ = "autores_git"
    __table_args__ = (
        UniqueConstraint("equipo_id", "email", name="uq_autores_git_equipo_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipo_id: Mapped[int] = mapped_column(ForeignKey("equipos.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    nombre_git: Mapped[str] = mapped_column(String(255), nullable=False)
    commits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estudiante_id: Mapped[int | None] = mapped_column(
        ForeignKey("estudiantes.id"), nullable=True
    )
    ignorado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    equipo: Mapped[Equipo] = relationship(back_populates="autores_git")
    estudiante: Mapped[Estudiante | None] = relationship(back_populates="autores_git")


class Rubrica(Base):
    __tablename__ = "rubricas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    codigo: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    orden: Mapped[int] = mapped_column(Integer, nullable=False)
    guia: Mapped[str | None] = mapped_column(Text, nullable=True)

    criterios: Mapped[list[CriterioEvaluacion]] = relationship(
        back_populates="rubrica"
    )


class CriterioEvaluacion(Base):
    __tablename__ = "criterios_evaluacion"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rubrica_id: Mapped[int] = mapped_column(
        ForeignKey("rubricas.id"), nullable=False
    )
    codigo: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    nombre: Mapped[str] = mapped_column(String(255), nullable=False)
    peso: Mapped[float] = mapped_column(Float, nullable=False)
    orden: Mapped[int] = mapped_column(Integer, nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    que_revisar: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidencia_adecuado: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidencia_solido: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidencia_destacado: Mapped[str | None] = mapped_column(Text, nullable=True)
    ejemplo_observacion: Mapped[str | None] = mapped_column(Text, nullable=True)

    rubrica: Mapped[Rubrica] = relationship(back_populates="criterios")
    valoraciones: Mapped[list[Valoracion]] = relationship(
        back_populates="criterio", cascade="all, delete-orphan"
    )


class Evaluacion(Base):
    __tablename__ = "evaluaciones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipo_id: Mapped[int] = mapped_column(
        ForeignKey("equipos.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    evaluador: Mapped[str] = mapped_column(String(255), nullable=False)
    actualizado: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    equipo: Mapped[Equipo] = relationship(back_populates="evaluacion")
    valoraciones: Mapped[list[Valoracion]] = relationship(
        back_populates="evaluacion", cascade="all, delete-orphan"
    )


class Valoracion(Base):
    __tablename__ = "valoraciones"
    __table_args__ = (
        CheckConstraint("nivel BETWEEN 0 AND 5", name="ck_valoraciones_nivel_range"),
        UniqueConstraint(
            "evaluacion_id", "criterio_id", name="uq_valoraciones_evaluacion_criterio"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evaluacion_id: Mapped[int] = mapped_column(
        ForeignKey("evaluaciones.id", ondelete="CASCADE"), nullable=False
    )
    criterio_id: Mapped[int] = mapped_column(
        ForeignKey("criterios_evaluacion.id"), nullable=False
    )
    nivel: Mapped[int] = mapped_column(Integer, nullable=False)
    observacion: Mapped[str | None] = mapped_column(Text, nullable=True)

    evaluacion: Mapped[Evaluacion] = relationship(back_populates="valoraciones")
    criterio: Mapped[CriterioEvaluacion] = relationship(back_populates="valoraciones")


class ConfigEvaluacion(Base):
    __tablename__ = "config_evaluacion"

    clave: Mapped[str] = mapped_column(String(100), primary_key=True)
    valor: Mapped[str] = mapped_column(String(255), nullable=False)
