from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
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
    nombre_proyecto: Mapped[str] = mapped_column(String(255), nullable=False)
    url_github: Mapped[str] = mapped_column(String(2048), nullable=False)
    estado_salud: Mapped[str | None] = mapped_column(String(100), nullable=True)

    comision: Mapped[Comision] = relationship(back_populates="equipos")
    estudiantes: Mapped[list[Estudiante]] = relationship(back_populates="equipo")
    metricas: Mapped[list[MetricaProyecto]] = relationship(back_populates="equipo")


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


class MetricaProyecto(Base):
    __tablename__ = "metricas_proyecto"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipo_id: Mapped[int] = mapped_column(ForeignKey("equipos.id"), nullable=False)
    fecha: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    total_commits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issues_abiertos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prs_abiertos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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

    estudiante: Mapped[Estudiante] = relationship(back_populates="metricas")
