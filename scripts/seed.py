"""Aplica las migraciones y carga los datos ficticios de la demo.

Uso:
    python scripts/seed.py            # base vacía
    python scripts/seed.py --reset    # borra todo y vuelve a poblar (solo para la demo)
    python scripts/seed.py --si-hace-falta 7
        # al arrancar el servidor: migra; puebla si la base está vacía y, en modo demo,
        # vuelve a poblar si los datos tienen más de 7 días (así las fechas de la demo siguen al día)

La contraseña de los usuarios de demo se toma de DEMO_PASSWORD.
"""

import argparse
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import create_session_factory  # noqa: E402
from app.seed import poblar  # noqa: E402


def _que_hacer(settings, dias: int) -> str:
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.models import BitacoraAuditoria, Salon
    from app.tiempo import ahora, aware

    with create_session_factory(settings.database_url)() as db:
        if not db.scalar(select(func.count()).select_from(Salon)):
            return "poblar"
        if not settings.demo_mode:
            return "nada"
        poblada = db.scalar(select(func.max(BitacoraAuditoria.creada_en)).where(BitacoraAuditoria.accion == "poblar_demo"))
    return "renovar" if poblada is None or aware(poblada) < ahora() - timedelta(days=dias) else "nada"


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga datos ficticios para la demo.")
    parser.add_argument("--reset", action="store_true", help="Borra todas las tablas antes de poblar.")
    parser.add_argument("--si-hace-falta", type=int, metavar="DIAS",
                        help="Puebla solo si la base está vacía o, en modo demo, si los datos tienen más de DIAS días.")
    args = parser.parse_args()

    settings = get_settings()
    password = os.environ.get("DEMO_PASSWORD") or settings.demo_password
    if len(password) < 10:
        print("Defina DEMO_PASSWORD con al menos 10 caracteres.", file=sys.stderr)
        return 1

    alembic_cfg = Config(str(RAIZ / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    if args.si_hace_falta is not None:
        command.upgrade(alembic_cfg, "head")
        accion = _que_hacer(settings, args.si_hace_falta)
        if accion == "nada":
            print("Migraciones al día; la demo ya tiene datos recientes.")
            return 0
        args.reset = accion == "renovar"
        print("Renovando los datos de la demo…" if args.reset else "Base vacía: cargando los datos de la demo…")

    if args.reset:
        if settings.app_env == "production" and not settings.demo_mode:
            print("--reset está bloqueado en producción salvo con DEMO_MODE=true.", file=sys.stderr)
            return 1
        command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")

    with create_session_factory(settings.database_url)() as db:
        try:
            resumen = poblar(db, settings=settings, password=password)
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1

    print(f"Salones: {resumen['salones']} · Alumnos: {resumen['alumnos']} · Familias: {resumen['familias']}")
    print(f"Usuarios: {resumen['usuarios']} (docentes: {resumen['docentes']}, "
          f"estudiantes con cuenta: {resumen['estudiantes_con_cuenta']})")
    print(f"Campañas: {resumen['campanas']} · Respuestas: {resumen['respuestas']}")
    print(f"Tareas: {resumen['tareas']} · Entregas: {resumen['entregas']}")
    print("\nUsuarios de demostración (contraseña: DEMO_PASSWORD):")
    for login in ("admin", "sofia", "rector", "dir.matematica", "docente", "representante", "estudiante",
                  "consejeria", "auditor", "porteria"):
        print(f"  {login}")
    print("\nEnlaces de familias de ejemplo (se muestran una sola vez):")
    for familia, url in resumen["enlaces"]:
        print(f"  {familia}: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
