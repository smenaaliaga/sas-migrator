"""
Limpia artefactos de state/ y output/ para reiniciar la migración desde cero.
Preserva .gitkeep y recrea state/nodes/.

Uso: python .github/skills/migration-state/scripts/reset.py
     (ejecutar desde la raíz del workspace)
"""
import os
import shutil

base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def clean_dir(folder):
    count = 0
    path = os.path.join(base, folder)
    if not os.path.exists(path):
        return 0
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            if f == ".gitkeep":
                continue
            fp = os.path.join(root, f)
            os.remove(fp)
            count += 1
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                os.rmdir(dp)
            except OSError:
                pass
    return count


n_state = clean_dir("state")
n_output = clean_dir("output")

os.makedirs(os.path.join(base, "state", "nodes"), exist_ok=True)

print(f"Reset completado: {n_state} archivos en state/, {n_output} archivos en output/ eliminados.")
print("state/nodes/ recreado.")
