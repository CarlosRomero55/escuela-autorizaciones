from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=160)
    password: str = Field(min_length=1, max_length=200)


class TotpIn(BaseModel):
    codigo: str = Field(pattern=r"^\d{6}$")


class SalonIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=60)
    grado: str = Field(min_length=1, max_length=20)


class SalonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre: str
    grado: str


class AlumnoIn(BaseModel):
    codigo_interno: str = Field(min_length=1, max_length=20)
    nombre: str = Field(min_length=1, max_length=120)
    salon_id: int


class AlumnoPatch(BaseModel):
    activo: bool | None = None


class AlumnoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    codigo_interno: str
    nombre: str
    salon_id: int
    activo: bool


class FamiliaIn(BaseModel):
    nombre_familia: str = Field(min_length=1, max_length=120)
    telefono: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=160)
    consentimiento_mensajes: bool = False


class FamiliaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nombre_familia: str
    telefono: str | None
    email: str | None
    consentimiento_mensajes: bool


class AdultoIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    parentesco: str = Field(min_length=1, max_length=40)
    telefono: str | None = Field(default=None, max_length=20)
    familia_id: int | None = None


class AdultoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    alumno_id: int
    nombre: str
    parentesco: str
    estado: str
    temporal: bool
    vigente_desde: datetime
    vigente_hasta: datetime | None


class EstadoAdultoIn(BaseModel):
    estado: Literal["validado", "rechazado"]


class CampanaIn(BaseModel):
    titulo: str = Field(min_length=1, max_length=160)
    descripcion: str | None = None
    tipo: Literal["salida", "vacuna", "evento", "otro"]
    texto_legal: str = Field(min_length=1)
    fecha_limite: datetime
    salon_ids: list[int] = Field(min_length=1)


class CampanaOut(BaseModel):
    id: int
    titulo: str
    tipo: str
    fecha_limite: datetime
    salon_ids: list[int]
