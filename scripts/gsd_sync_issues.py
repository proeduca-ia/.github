#!/usr/bin/env python3
"""
gsd_sync_issues.py — Sincroniza los PLAN.md de GSD con GitHub Issues.

Uso:
    python scripts/gsd_sync_issues.py [opciones]

Opciones:
    --dry-run       Muestra qué haría sin llamar a la API
    --phase FASE    Sincroniza solo una fase (ej. phase-1 o fase-1)
    --close-done    Cierra issues cuya tarea ya no existe en PLAN.md

Variables de entorno requeridas:
    GITHUB_TOKEN    Token con permisos: issues:write, metadata:read
    GITHUB_REPO     owner/repo  (ej. proeduca-ia/svc-conversa-nueva-revista)

Estado persistente:
    .github/gsd-issues-state.json   (commiteado en el repo, nunca borrar)

Flujo:
    1. Busca todos los .planning/**/PLAN.md del repo
    2. Parsea milestones y tasks con el formato estándar de GSD
    3. Crea GitHub Milestones para cada GSD Milestone (si no existen)
    4. Crea labels de fase y tipo (si no existen)
    5. Crea GitHub Issues para cada task (idempotente: no duplica)
    6. Actualiza el estado en .github/gsd-issues-state.json
"""

import os
import re
import json
import sys
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Modelos de datos
# ---------------------------------------------------------------------------

@dataclass
class GSDTask:
    task_id: str            # ej. M0-T1
    title: str
    duration_h: float
    description: str
    dependencies: str
    done_criteria: str
    files: list
    milestone_id: str       # ej. M0
    milestone_title: str
    phase_id: str           # ej. phase-1 o fase-1
    phase_number: str       # ej. 1
    phase_title: str
    plan_file: str          # ruta relativa al PLAN.md

    @property
    def global_id(self) -> str:
        """Clave única para el estado persistente."""
        return f"{self.phase_id}/{self.task_id}"

    @property
    def issue_title(self) -> str:
        return f"[{self.phase_id.upper()}] [{self.task_id}] {self.title}"


@dataclass
class GSDMilestone:
    milestone_id: str       # ej. M0
    title: str
    objective: str
    duration_h: float
    phase_id: str
    phase_number: str
    phase_title: str
    tasks: list = field(default_factory=list)

    @property
    def github_milestone_title(self) -> str:
        return f"[{self.phase_id.upper()}] {self.milestone_id}: {self.title}"


# ---------------------------------------------------------------------------
# Parser de PLAN.md
# ---------------------------------------------------------------------------

def _extract_field(text: str, field_name: str) -> str:
    """Extrae el valor de '- **FieldName:** valor' en el texto."""
    # Acepta variantes en inglés y español, con o sin negrita
    pattern = rf'\*\*{re.escape(field_name)}[:\*]*\*?\*?\s*(.+?)(?=\n\s*[-*]|\n\s*\*\*|\Z)'
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip().split('\n')[0].strip()
    return ""


def _extract_files(task_body: str) -> list:
    """Extrae la lista de archivos de la sección **Archivos** o **Files**."""
    # Busca la sección de archivos
    m = re.search(
        r'\*\*(?:Archivos|Files)[:\*]*\*?\*?\s*\n((?:\s*[-*]\s+.+\n?)+)',
        task_body,
        re.IGNORECASE
    )
    if not m:
        return []
    lines = m.group(1).strip().split('\n')
    files = []
    for line in lines:
        line = line.strip().lstrip('-* ').strip()
        if line and not line.startswith('**'):
            # Quita anotaciones como "(actualizar)", "(nuevo)"
            line = re.sub(r'\s*\(.*?\)', '', line).strip()
            if line:
                files.append(line)
    return files


def _parse_duration(text: str) -> float:
    """Extrae horas de texto como '1.5h' o '2 horas'."""
    m = re.search(r'([\d.]+)\s*h', text, re.IGNORECASE)
    return float(m.group(1)) if m else 0.0


def _phase_number_from_id(phase_id: str) -> str:
    """Extrae el número de fase de 'phase-1' o 'fase-1' → '1'."""
    m = re.search(r'(\d+(?:\.\d+)?)', phase_id)
    return m.group(1) if m else phase_id


def parse_plan_file(plan_path: Path, repo_root: Path) -> tuple:
    """
    Parsea un PLAN.md de GSD y devuelve (milestones, tasks).
    Compatible con el formato estándar generado por gsd-planner.
    """
    content = plan_path.read_text(encoding='utf-8')
    relative_path = str(plan_path.relative_to(repo_root))

    # Determinar phase_id desde la ruta: .planning/phase-1/PLAN.md
    parts = plan_path.parts
    try:
        planning_idx = next(i for i, p in enumerate(parts) if p == '.planning')
        phase_id = parts[planning_idx + 1]
    except StopIteration:
        phase_id = "phase-unknown"

    phase_number = _phase_number_from_id(phase_id)

    # Título de la fase desde el H1
    phase_title_m = re.search(r'^#\s+(.+)', content, re.MULTILINE)
    phase_title = phase_title_m.group(1).strip() if phase_title_m else phase_id

    milestones = []
    tasks = []
    current_milestone: Optional[GSDMilestone] = None

    lines = content.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]

        # --- Detectar MILESTONE ---
        # Formato: ### **MILESTONE N: Título** o ### MILESTONE N: Título
        # También acepta variantes en inglés: ### **M0: Título**
        milestone_m = re.match(
            r'^###\s+\*{0,2}(?:MILESTONE\s+)?(\d+):\s+(.+?)\*{0,2}\s*$',
            line
        )
        if milestone_m:
            m_num = milestone_m.group(1)
            m_title = milestone_m.group(2).strip()
            milestone_id = f"M{m_num}"

            # Buscar Objetivo/Goal en las líneas siguientes
            objective = ""
            duration_h = 0.0
            j = i + 1
            while j < len(lines) and not lines[j].startswith('####') and not lines[j].startswith('###'):
                obj_m = re.search(r'\*\*(?:Objetivo|Goal|Objective)[:\*]*\*?\*?\s*(.+)', lines[j], re.IGNORECASE)
                if obj_m:
                    objective = obj_m.group(1).strip()
                dur_m = re.search(r'\*\*(?:Duración total|Total duration)[:\*]*\*?\*?\s*~?([\d.]+)\s*h', lines[j], re.IGNORECASE)
                if dur_m:
                    duration_h = float(dur_m.group(1))
                j += 1

            current_milestone = GSDMilestone(
                milestone_id=milestone_id,
                title=m_title,
                objective=objective,
                duration_h=duration_h,
                phase_id=phase_id,
                phase_number=phase_number,
                phase_title=phase_title,
            )
            milestones.append(current_milestone)
            i += 1
            continue

        # --- Detectar Task ---
        # Formato: #### Task M0-T1: Título  (también acepta #### M0-T1: Título)
        task_m = re.match(
            r'^####\s+(?:Task\s+)?(M\d+-T\d+):\s+(.+)$',
            line,
            re.IGNORECASE
        )
        if task_m:
            task_id = task_m.group(1).upper()
            task_title = task_m.group(2).strip()

            # Recoger el cuerpo de la task hasta el siguiente ####, ###, o ---
            task_lines = []
            j = i + 1
            while j < len(lines):
                if (lines[j].startswith('####') or
                        lines[j].startswith('###') or
                        re.match(r'^---+\s*$', lines[j])):
                    break
                task_lines.append(lines[j])
                j += 1

            task_body = '\n'.join(task_lines)

            description = _extract_field(task_body, 'Descripción') or _extract_field(task_body, 'Description')
            dependencies = _extract_field(task_body, 'Dependencias') or _extract_field(task_body, 'Dependencies') or 'None'
            done_criteria = _extract_field(task_body, 'Done')
            duration_raw = _extract_field(task_body, 'Duración') or _extract_field(task_body, 'Duration')
            duration_h = _parse_duration(duration_raw)
            files = _extract_files(task_body)

            # Si no hay milestone activo, crear uno genérico
            if current_milestone is None:
                current_milestone = GSDMilestone(
                    milestone_id='M0',
                    title='General',
                    objective='',
                    duration_h=0,
                    phase_id=phase_id,
                    phase_number=phase_number,
                    phase_title=phase_title,
                )
                milestones.append(current_milestone)

            task = GSDTask(
                task_id=task_id,
                title=task_title,
                duration_h=duration_h,
                description=description,
                dependencies=dependencies,
                done_criteria=done_criteria,
                files=files,
                milestone_id=current_milestone.milestone_id,
                milestone_title=current_milestone.title,
                phase_id=phase_id,
                phase_number=phase_number,
                phase_title=phase_title,
                plan_file=relative_path,
            )
            tasks.append(task)
            current_milestone.tasks.append(task)
            i = j
            continue

        i += 1

    return milestones, tasks


# ---------------------------------------------------------------------------
# GitHub API (urllib, sin dependencias externas)
# ---------------------------------------------------------------------------

class GitHubAPI:
    BASE = "https://api.github.com"

    def __init__(self, token: str, repo: str, dry_run: bool = False):
        self.token = token
        self.repo = repo
        self.dry_run = dry_run
        self._milestones_cache: Optional[dict] = None
        self._labels_cache: Optional[set] = None

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self.BASE}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            }
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode()
            raise RuntimeError(f"GitHub API {method} {url} → {e.code}: {body_text}")

    def _paginate(self, path: str) -> list:
        results = []
        page = 1
        while True:
            sep = '&' if '?' in path else '?'
            batch = self._request("GET", f"{path}{sep}per_page=100&page={page}")
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    # --- Milestones ---

    def get_milestones(self) -> dict:
        """Devuelve {title: {number, id}} de todos los milestones abiertos."""
        if self._milestones_cache is None:
            items = self._paginate(f"/repos/{self.repo}/milestones?state=open")
            self._milestones_cache = {m['title']: m for m in items}
        return self._milestones_cache

    def ensure_milestone(self, title: str, description: str = "") -> int:
        """Crea el milestone si no existe y devuelve su número."""
        milestones = self.get_milestones()
        if title in milestones:
            return milestones[title]['number']
        if self.dry_run:
            print(f"  [DRY-RUN] Crearía milestone: {title!r}")
            return 0
        result = self._request("POST", f"/repos/{self.repo}/milestones", {
            "title": title,
            "description": description,
        })
        self._milestones_cache[title] = result
        print(f"  Milestone creado: {title!r} (#{result['number']})")
        return result['number']

    # --- Labels ---

    def get_labels(self) -> set:
        """Devuelve el set de nombres de labels existentes."""
        if self._labels_cache is None:
            items = self._paginate(f"/repos/{self.repo}/labels")
            self._labels_cache = {l['name'] for l in items}
        return self._labels_cache

    def ensure_label(self, name: str, color: str, description: str = "") -> None:
        """Crea el label si no existe."""
        if name in self.get_labels():
            return
        if self.dry_run:
            print(f"  [DRY-RUN] Crearía label: {name!r}")
            return
        try:
            self._request("POST", f"/repos/{self.repo}/labels", {
                "name": name,
                "color": color,
                "description": description,
            })
            self._labels_cache.add(name)
            print(f"  Label creado: {name!r}")
        except RuntimeError as e:
            if "already_exists" in str(e):
                self._labels_cache.add(name)
            else:
                raise

    # --- Issues ---

    def create_issue(self, title: str, body: str, labels: list,
                     milestone_number: int) -> dict:
        if self.dry_run:
            print(f"  [DRY-RUN] Crearía issue: {title!r}")
            return {"number": 0, "html_url": "dry-run"}
        payload = {"title": title, "body": body, "labels": labels}
        if milestone_number:
            payload["milestone"] = milestone_number
        result = self._request("POST", f"/repos/{self.repo}/issues", payload)
        return result

    def update_issue(self, number: int, title: str, body: str,
                     labels: list, milestone_number: int) -> dict:
        if self.dry_run:
            print(f"  [DRY-RUN] Actualizaría issue #{number}: {title!r}")
            return {"number": number, "html_url": "dry-run"}
        payload = {"title": title, "body": body, "labels": labels}
        if milestone_number:
            payload["milestone"] = milestone_number
        return self._request("PATCH", f"/repos/{self.repo}/issues/{number}", payload)


# ---------------------------------------------------------------------------
# Generación del cuerpo del issue
# ---------------------------------------------------------------------------

LABEL_COLORS = {
    "gsd": "0075ca",
    "gsd:task": "e4e669",
    "gsd:milestone": "d4edda",
    "type:task": "bfd4f2",
    "type:epic": "7057ff",
}

PHASE_COLORS = [
    "b60205", "d93f0b", "e99695", "f9d0c4",
    "fef2c0", "c2e0c6", "bfd4f2", "c5def5",
]


def _phase_label(phase_id: str) -> str:
    return f"phase:{phase_id}"


def _milestone_label(milestone_id: str) -> str:
    return f"milestone:{milestone_id.lower()}"


def build_issue_body(task: GSDTask) -> str:
    files_md = ""
    if task.files:
        files_md = "\n### Archivos afectados\n" + "\n".join(f"- `{f}`" for f in task.files)

    done_md = ""
    if task.done_criteria:
        done_md = f"\n### Criterio de Done\n{task.done_criteria}"

    return f"""\
## {task.task_id}: {task.title}

| Campo | Valor |
|-------|-------|
| **Fase** | {task.phase_id} — {task.phase_title} |
| **Milestone GSD** | {task.milestone_id}: {task.milestone_title} |
| **Duración estimada** | {task.duration_h}h |
| **Dependencias** | {task.dependencies} |

### Descripción
{task.description or '_Sin descripción_'}
{files_md}
{done_md}

---
> Generado por [gsd-sync-issues](../../scripts/gsd_sync_issues.py) · Task `{task.global_id}` · `{task.plan_file}`
"""


# ---------------------------------------------------------------------------
# Estado persistente
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding='utf-8'))
    return {"version": "1", "issues": {}}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding='utf-8')


# ---------------------------------------------------------------------------
# Sincronización principal
# ---------------------------------------------------------------------------

def sync(api: GitHubAPI, tasks: list, milestones_gsd: list,
         state: dict, phase_filter: Optional[str] = None) -> dict:
    """Sincroniza tareas con GitHub Issues. Devuelve el estado actualizado."""

    # Asegurar labels base
    api.ensure_label("gsd", "0075ca", "Generado por GSD")
    api.ensure_label("gsd:task", "e4e669", "Tarea atómica de GSD")

    # Agrupar milestones por fase
    milestones_by_key: dict = {}
    for ms in milestones_gsd:
        if phase_filter and ms.phase_id != phase_filter:
            continue
        key = (ms.phase_id, ms.milestone_id)
        milestones_by_key[key] = ms

    # Crear GitHub Milestones y labels de fase
    gh_milestone_numbers: dict = {}
    seen_phases = set()

    for key, ms in milestones_by_key.items():
        phase_id = ms.phase_id

        if phase_id not in seen_phases:
            color_idx = int(ms.phase_number.split('.')[0]) % len(PHASE_COLORS)
            api.ensure_label(
                _phase_label(phase_id),
                PHASE_COLORS[color_idx],
                f"Fase {ms.phase_number}: {ms.phase_title}"
            )
            api.ensure_label("type:task", "bfd4f2", "Tarea atómica")
            seen_phases.add(phase_id)

        gh_ms_title = ms.github_milestone_title
        gh_ms_number = api.ensure_milestone(gh_ms_title, ms.objective)
        gh_milestone_numbers[key] = gh_ms_number
        api.ensure_label(
            _milestone_label(ms.milestone_id),
            "d4edda",
            f"{ms.milestone_id}: {ms.title}"
        )

    # Crear/actualizar issues por tarea
    issues_state = state.get("issues", {})

    for task in tasks:
        if phase_filter and task.phase_id != phase_filter:
            continue

        global_id = task.global_id
        labels = [
            "gsd",
            "gsd:task",
            "type:task",
            _phase_label(task.phase_id),
            _milestone_label(task.milestone_id),
        ]
        key = (task.phase_id, task.milestone_id)
        ms_number = gh_milestone_numbers.get(key, 0)
        body = build_issue_body(task)

        if global_id in issues_state:
            # Ya existe: actualizar si el título cambió
            existing = issues_state[global_id]
            if existing.get("title") != task.issue_title:
                result = api.update_issue(
                    existing["issue_number"],
                    task.issue_title, body, labels, ms_number
                )
                issues_state[global_id]["title"] = task.issue_title
                print(f"  Issue actualizado #{existing['issue_number']}: {task.issue_title}")
            else:
                print(f"  Issue ya existe #{existing['issue_number']}: {task.task_id} (sin cambios)")
        else:
            # Crear nuevo issue
            result = api.create_issue(task.issue_title, body, labels, ms_number)
            issues_state[global_id] = {
                "issue_number": result["number"],
                "title": task.issue_title,
                "url": result["html_url"],
                "phase_id": task.phase_id,
                "milestone_id": task.milestone_id,
            }
            print(f"  Issue creado #{result['number']}: {task.issue_title}")

    state["issues"] = issues_state
    return state


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sincroniza PLAN.md de GSD con GitHub Issues")
    parser.add_argument("--dry-run", action="store_true", help="Sin llamadas reales a la API")
    parser.add_argument("--phase", help="Sincronizar solo esta fase (ej. phase-1)")
    parser.add_argument("--repo-root", default=".", help="Raíz del repositorio (default: .)")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPO")

    if not token:
        print("ERROR: Define GITHUB_TOKEN o GH_TOKEN", file=sys.stderr)
        sys.exit(1)
    if not repo:
        print("ERROR: Define GITHUB_REPO (ej. 'org/mi-repo')", file=sys.stderr)
        sys.exit(1)

    repo_root = Path(args.repo_root).resolve()
    planning_dir = repo_root / ".planning"
    state_path = repo_root / ".github" / "gsd-issues-state.json"

    if not planning_dir.exists():
        print(f"ERROR: No existe {planning_dir}", file=sys.stderr)
        sys.exit(1)

    # Buscar todos los PLAN.md
    plan_files = sorted(planning_dir.glob("**/PLAN.md"))
    if not plan_files:
        print("No se encontraron archivos PLAN.md en .planning/")
        sys.exit(0)

    # Parsear
    all_milestones = []
    all_tasks = []
    for plan_file in plan_files:
        phase_id = plan_file.parent.name
        if args.phase and phase_id != args.phase:
            continue
        print(f"\nParsing: {plan_file.relative_to(repo_root)}")
        ms, tasks = parse_plan_file(plan_file, repo_root)
        print(f"  {len(ms)} milestones, {len(tasks)} tasks encontrados")
        all_milestones.extend(ms)
        all_tasks.extend(tasks)

    if not all_tasks:
        print("No se encontraron tasks para sincronizar.")
        sys.exit(0)

    # Sincronizar
    api = GitHubAPI(token=token, repo=repo, dry_run=args.dry_run)
    state = load_state(state_path)

    print(f"\nSincronizando {len(all_tasks)} tasks con {repo}...")
    if args.dry_run:
        print("(modo dry-run: no se harán cambios reales)\n")

    state = sync(api, all_tasks, all_milestones, state, phase_filter=args.phase)

    if not args.dry_run:
        save_state(state_path, state)
        print(f"\nEstado guardado en {state_path.relative_to(repo_root)}")

    print("\nSincronizacion completada.")


if __name__ == "__main__":
    main()
