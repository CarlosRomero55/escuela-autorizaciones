"""Aplica las migraciones y carga los datos ficticios de la demo.

Uso:
    python scripts/seed.py            # base vacía
    python scripts/seed.py --reset    # borra todo y vuelve a poblar (solo para la demo)

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga datos ficticios para la demo.")
    parser.add_argument("--reset", action="store_true", help="Borra todas las tablas antes de poblar.")
    args = parser.parse_args()

    settings = get_settings()
    password = os.environ.get("DEMO_PASSWORD") or settings.demo_password
    if len(password) < 10:
        print("Defina DEMO_PASSWORD con al menos 10 caracteres.", file=sys.stderr)
        return 1

    alembic_cfg = Config(str(RAIZ / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
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
    print(f"Adultos: {resumen['adultos']} · Campañas: {resumen['campanas']} · Respuestas: {resumen['respuestas']}")
    print("\nUsuarios (misma contraseña de DEMO_PASSWORD):")
    for email, rol in resumen["usuarios"]:
        print(f"  {email:24} {rol}")
    print("\nEnlaces de familias de ejemplo (se muestran una sola vez):")
    for familia, url in resumen["enlaces"]:
        print(f"  {familia}: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
