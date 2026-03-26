#!/usr/bin/env python3
"""
gsd_sync_issues.py — Sincroniza los PLAN.md de GSD con GitHub Issues.

Uso:
    python scripts/gsd_sync_issues.py [opciones]

Opciones:
    --dry-run       Muestra qué haría sin llamar a la API
    --phase FASE    Sincroniza solo una fase (nombre del directorio de fase)

Variables de entorno requeridas:
    GITHUB_TOKEN    Token con permisos: issues:write, metadata:read
    GITHUB_REPO     owner/repo  (ej. proeduca-ia/svc-conversa-nueva-revista)

Estado persistente:
    .github/gsd-issues-state.json   (commiteado en el repo, nunca borrar)

Formato GSD soportado:
    .planning/phases/{phase-slug}/{NN}-{PP}-PLAN.md
    - YAML frontmatter: phase, plan, wave, depends_on, requirements, must_haves
    - XML tasks: <task type="auto"><name>...</name>...</task>
    Tambien soporta formato legacy: .planning/{phase-dir}/PLAN.md
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
    task_id: str            # ej. 01-01-T1
    name: str
    plan_id: str            # ej. 01-01
    phase_slug: str         # ej. 01-foundations-bis-contract-and-security-baseline
    phase_number: str       # ej. 01
    phase_title: str        # ej. Foundations, BIS Contract, and Security Baseline
    wave: int
    requirements: list
    files: list
    plan_file: str

    @property
    def global_id(self) -> str:
        return f"{self.phase_slug}/{self.task_id}"

    @property
    def issue_title(self) -> str:
        return f"[P{self.phase_number}] [{self.plan_id}] {self.name}"


@dataclass
class GSDPlan:
    plan_id: str
    phase_slug: str
    phase_number: str
    phase_title: str
    wave: int
    depends_on: list
    requirements: list
    objective: str
    must_haves: list
    tasks: list = field(default_factory=list)
    plan_file: str = ""


# ---------------------------------------------------------------------------
# Parser de PLAN.md (YAML frontmatter + XML tasks)
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> dict:
    """Extrae el frontmatter YAML entre --- delimiters."""
    m = re.match(r'^---\s*\n(.+?)\n---', content, re.DOTALL)
    if not m:
        return {}
    fm = {}
    raw = m.group(1)
    current_key = None

    for line in raw.split('\n'):
        list_item = re.match(r'^\s+-\s+(.+)', line)
        kv = re.match(r'^(\w[\w_]*)\s*:\s*(.+)?', line)

        if kv:
            key = kv.group(1)
            val = kv.group(2).strip() if kv.group(2) else None
            if val and val.startswith('[') and val.endswith(']'):
                items = re.findall(r'"([^"]+)"', val)
                fm[key] = items
            elif val is None:
                fm[key] = {}
                current_key = key
            else:
                fm[key] = val
                current_key = key
        elif list_item and current_key:
            if current_key not in fm or not isinstance(fm[current_key], list):
                fm[current_key] = []
            val = list_item.group(1).strip().strip('"')
            fm[current_key].append(val)

    return fm


def _extract_objective(content: str) -> str:
    """Extrae el texto dentro de <objective>...</objective>."""
    m = re.search(r'<objective>\s*(.+?)\s*</objective>', content, re.DOTALL)
    return m.group(1).strip().split('\n')[0] if m else ""


def _extract_tasks(content: str) -> list:
    """Extrae tasks de <task>...</task> blocks."""
    tasks = []
    pattern = re.compile(
        r'<task[^>]*>\s*<name>(.+?)</name>\s*(?:<files>\s*(.+?)\s*</files>)?',
        re.DOTALL
    )
    for i, m in enumerate(pattern.finditer(content), 1):
        name = m.group(1).strip()
        files_raw = m.group(2) or ""
        files = [f.strip() for f in files_raw.strip().split('\n') if f.strip()]
        tasks.append({"number": i, "name": name, "files": files})
    return tasks


def _extract_must_have_truths(content: str) -> list:
    """Extrae los truths de must_haves del frontmatter."""
    truths = []
    in_truths = False
    for line in content.split('\n'):
        if 'truths:' in line:
            in_truths = True
            continue
        if in_truths:
            m = re.match(r'^\s+-\s+"(.+)"', line)
            if m:
                truths.append(m.group(1))
            elif re.match(r'^\s+\w', line) and ':' in line:
                in_truths = False
            elif re.match(r'^\w', line):
                in_truths = False
    return truths


def _phase_title_from_slug(slug: str) -> str:
    """'01-foundations-bis-contract' → 'Foundations Bis Contract'."""
    parts = slug.split('-')[1:]
    title = ' '.join(p.capitalize() for p in parts)
    # Fix common acronyms
    for acr in ['Bis', 'Cms', 'Rag', 'Bis', 'Api', 'Sse', 'Jwt', 'Crud']:
        title = title.replace(acr, acr.upper())
    return title


def parse_plan_file(plan_path: Path, repo_root: Path) -> Optional[GSDPlan]:
    """Parsea un *-PLAN.md de GSD y devuelve un GSDPlan con sus tasks."""
    content = plan_path.read_text(encoding='utf-8')
    relative_path = str(plan_path.relative_to(repo_root))

    fm = _parse_frontmatter(content)
    if not fm:
        return None

    phase_slug = fm.get('phase', plan_path.parent.name)
    plan_num = fm.get('plan', '01')
    phase_number = phase_slug.split('-')[0] if '-' in phase_slug else '00'
    plan_id = f"{phase_number}-{str(plan_num).zfill(2)}"
    wave = int(fm.get('wave', 1))
    depends_on = fm.get('depends_on', [])
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    requirements = fm.get('requirements', [])
    if isinstance(requirements, str):
        requirements = [requirements]

    objective = _extract_objective(content)
    must_haves = _extract_must_have_truths(content)
    raw_tasks = _extract_tasks(content)

    phase_title = _phase_title_from_slug(phase_slug)

    plan = GSDPlan(
        plan_id=plan_id,
        phase_slug=phase_slug,
        phase_number=phase_number,
        phase_title=phase_title,
        wave=wave,
        depends_on=depends_on,
        requirements=requirements,
        objective=objective,
        must_haves=must_haves,
        plan_file=relative_path,
    )

    for t in raw_tasks:
        task_id = f"{plan_id}-T{t['number']}"
        task = GSDTask(
            task_id=task_id,
            name=t['name'],
            plan_id=plan_id,
            phase_slug=phase_slug,
            phase_number=phase_number,
            phase_title=phase_title,
            wave=wave,
            requirements=requirements,
            files=t['files'],
            plan_file=relative_path,
        )
        plan.tasks.append(task)

    return plan


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
            url, data=data, method=method,
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

    def get_milestones(self) -> dict:
        if self._milestones_cache is None:
            items = self._paginate(f"/repos/{self.repo}/milestones?state=open")
            self._milestones_cache = {m['title']: m for m in items}
        return self._milestones_cache

    def ensure_milestone(self, title: str, description: str = "") -> int:
        milestones = self.get_milestones()
        if title in milestones:
            return milestones[title]['number']
        if self.dry_run:
            print(f"  [DRY-RUN] Crearía milestone: {title!r}")
            return 0
        result = self._request("POST", f"/repos/{self.repo}/milestones", {
            "title": title, "description": description,
        })
        self._milestones_cache[title] = result
        print(f"  Milestone creado: {title!r} (#{result['number']})")
        return result['number']

    def get_labels(self) -> set:
        if self._labels_cache is None:
            items = self._paginate(f"/repos/{self.repo}/labels")
            self._labels_cache = {l['name'] for l in items}
        return self._labels_cache

    def ensure_label(self, name: str, color: str, description: str = "") -> None:
        if name in self.get_labels():
            return
        if self.dry_run:
            print(f"  [DRY-RUN] Crearía label: {name!r}")
            return
        try:
            self._request("POST", f"/repos/{self.repo}/labels", {
                "name": name, "color": color, "description": description,
            })
            self._labels_cache.add(name)
            print(f"  Label creado: {name!r}")
        except RuntimeError as e:
            if "already_exists" in str(e):
                self._labels_cache.add(name)
            else:
                raise

    def create_issue(self, title: str, body: str, labels: list,
                     milestone_number: int) -> dict:
        if self.dry_run:
            print(f"  [DRY-RUN] Crearía issue: {title!r}")
            return {"number": 0, "html_url": "dry-run"}
        payload = {"title": title, "body": body, "labels": labels}
        if milestone_number:
            payload["milestone"] = milestone_number
        return self._request("POST", f"/repos/{self.repo}/issues", payload)

    def update_issue(self, number: int, title: str, body: str,
                     labels: list, milestone_number: int) -> dict:
        if self.dry_run:
            print(f"  [DRY-RUN] Actualizaría issue #{number}: {title!r}")
            return {"number": number, "html_url": "dry-run"}
        payload = {"title": title, "body": body, "labels": labels}
        if milestone_number:
            payload["milestone"] = milestone_number
        return self._request("PATCH", f"/repos/{self.repo}/issues/{number}", payload)

    def close_issue(self, number: int, reason: str = "not_planned") -> dict:
        """Cierra un issue. reason: 'completed' | 'not_planned'."""
        if self.dry_run:
            print(f"  [DRY-RUN] Cerraría issue #{number} ({reason})")
            return {"number": number}
        return self._request("PATCH", f"/repos/{self.repo}/issues/{number}", {
            "state": "closed",
            "state_reason": reason,
        })

    def comment_issue(self, number: int, body: str) -> dict:
        if self.dry_run:
            print(f"  [DRY-RUN] Comentaría issue #{number}")
            return {}
        return self._request("POST",
                             f"/repos/{self.repo}/issues/{number}/comments",
                             {"body": body})


# ---------------------------------------------------------------------------
# Generación del cuerpo del issue
# ---------------------------------------------------------------------------

PHASE_COLORS = [
    "b60205", "d93f0b", "e99695", "f9d0c4",
    "fef2c0", "c2e0c6", "bfd4f2", "c5def5",
]


def build_issue_body(task: GSDTask, plan: GSDPlan) -> str:
    files_md = ""
    if task.files:
        files_md = "\n### Archivos\n" + "\n".join(f"- `{f}`" for f in task.files)

    reqs_md = " / ".join(task.requirements) if task.requirements else "N/A"

    must_haves_md = ""
    if plan.must_haves:
        must_haves_md = "\n### Must-haves del plan\n" + "\n".join(
            f"- {mh}" for mh in plan.must_haves
        )

    return f"""\
## {task.task_id}: {task.name}

| Campo | Valor |
|-------|-------|
| **Phase** | {task.phase_number} — {task.phase_title} |
| **Plan** | {task.plan_id} (wave {task.wave}) |
| **Requirements** | {reqs_md} |

### Objetivo del plan
{plan.objective or '_Sin objetivo_'}
{files_md}
{must_haves_md}

---
> Generado por [gsd-sync-issues](https://github.com/proeduca-ia/.github/blob/main/scripts/gsd_sync_issues.py) | `{task.global_id}` | `{task.plan_file}`
"""


# ---------------------------------------------------------------------------
# Estado persistente
# ---------------------------------------------------------------------------

def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding='utf-8'))
    return {"version": "2", "issues": {}}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding='utf-8'
    )


# ---------------------------------------------------------------------------
# Sincronización principal
# ---------------------------------------------------------------------------

def sync(api: GitHubAPI, plans: list, state: dict) -> dict:
    api.ensure_label("gsd", "0075ca", "Generado por GSD")
    api.ensure_label("gsd:task", "e4e669", "Tarea atómica de GSD")

    # Milestones por fase
    phases_seen: dict = {}
    for plan in plans:
        if plan.phase_slug not in phases_seen:
            phases_seen[plan.phase_slug] = plan

    gh_milestone_numbers: dict = {}
    for slug, plan in phases_seen.items():
        ms_title = f"Phase {plan.phase_number}: {plan.phase_title}"
        ms_number = api.ensure_milestone(ms_title, plan.objective)
        gh_milestone_numbers[slug] = ms_number

        color_idx = int(plan.phase_number) % len(PHASE_COLORS)
        api.ensure_label(f"phase:{plan.phase_number}", PHASE_COLORS[color_idx],
                         f"Phase {plan.phase_number}: {plan.phase_title}")

    # Labels de wave
    for plan in plans:
        api.ensure_label(f"wave:{plan.wave}", "c5def5",
                         f"Wave {plan.wave} (parallel execution group)")

    # Labels de requirements
    reqs_seen = set()
    for plan in plans:
        for req in plan.requirements:
            if req not in reqs_seen:
                api.ensure_label(f"req:{req}", "d4edda", f"Requirement {req}")
                reqs_seen.add(req)

    # Crear/actualizar issues
    issues_state = state.get("issues", {})

    for plan in plans:
        ms_number = gh_milestone_numbers.get(plan.phase_slug, 0)

        for task in plan.tasks:
            global_id = task.global_id
            labels = [
                "gsd",
                "gsd:task",
                f"phase:{task.phase_number}",
                f"wave:{task.wave}",
            ]
            for req in task.requirements:
                labels.append(f"req:{req}")

            body = build_issue_body(task, plan)

            if global_id in issues_state:
                existing = issues_state[global_id]
                # Siempre actualizar el body (el PLAN puede haber cambiado)
                api.update_issue(
                    existing["issue_number"],
                    task.issue_title, body, labels, ms_number
                )
                if existing.get("status") == "closed":
                    # Reabrir si la task vuelve a aparecer
                    api._request("PATCH",
                                 f"/repos/{api.repo}/issues/{existing['issue_number']}",
                                 {"state": "open"})
                    issues_state[global_id]["status"] = "open"
                    print(f"  Issue reabierto #{existing['issue_number']}: {task.issue_title}")
                else:
                    issues_state[global_id]["title"] = task.issue_title
                    print(f"  Issue actualizado #{existing['issue_number']}: {task.task_id}")
            else:
                result = api.create_issue(task.issue_title, body, labels, ms_number)
                issues_state[global_id] = {
                    "issue_number": result["number"],
                    "title": task.issue_title,
                    "url": result.get("html_url", ""),
                    "phase": task.phase_slug,
                    "plan_id": task.plan_id,
                }
                print(f"  Issue creado #{result['number']}: {task.issue_title}")

    # Cerrar issues cuya task ya no existe en ningún PLAN.md
    current_global_ids = {t.global_id for p in plans for t in p.tasks}
    for global_id, info in list(issues_state.items()):
        if global_id not in current_global_ids and info.get("status") != "closed":
            # Solo cerrar si la fase de este issue está en los plans procesados
            issue_phase = info.get("phase", "")
            phases_in_sync = {p.phase_slug for p in plans}
            if issue_phase in phases_in_sync:
                api.comment_issue(
                    info["issue_number"],
                    "Task eliminada del PLAN.md — cerrando issue.\n\n"
                    f"> `{global_id}` ya no aparece en los planes de la fase."
                )
                api.close_issue(info["issue_number"], reason="not_planned")
                issues_state[global_id]["status"] = "closed"
                print(f"  Issue cerrado #{info['issue_number']}: {global_id} (task eliminada)")

    state["issues"] = issues_state
    return state


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sincroniza PLAN.md de GSD con GitHub Issues"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--phase",
                        help="Sincronizar solo esta fase (slug del directorio)")
    parser.add_argument("--repo-root", default=".")
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

    # Buscar PLAN.md en ambos formatos
    plan_files = sorted(planning_dir.glob("phases/**/*-PLAN.md"))
    plan_files += sorted(planning_dir.glob("**/PLAN.md"))

    # Eliminar duplicados
    seen = set()
    unique = []
    for pf in plan_files:
        if pf not in seen:
            seen.add(pf)
            unique.append(pf)
    plan_files = unique

    if not plan_files:
        print("No se encontraron archivos PLAN.md en .planning/")
        sys.exit(0)

    # Parsear
    all_plans = []
    for pf in plan_files:
        phase_dir = pf.parent.name
        if args.phase and phase_dir != args.phase and not phase_dir.startswith(args.phase):
            continue
        print(f"\nParsing: {pf.relative_to(repo_root)}")
        plan = parse_plan_file(pf, repo_root)
        if plan:
            print(f"  Plan {plan.plan_id}: {len(plan.tasks)} tasks, wave {plan.wave}, reqs: {plan.requirements}")
            all_plans.append(plan)
        else:
            print(f"  (sin frontmatter, ignorado)")

    all_tasks = [t for p in all_plans for t in p.tasks]
    if not all_tasks:
        print("\nNo se encontraron tasks para sincronizar.")
        sys.exit(0)

    api = GitHubAPI(token=token, repo=repo, dry_run=args.dry_run)
    state = load_state(state_path)

    print(f"\nSincronizando {len(all_tasks)} tasks de {len(all_plans)} planes con {repo}...")
    if args.dry_run:
        print("(modo dry-run)\n")

    state = sync(api, all_plans, state)

    if not args.dry_run:
        save_state(state_path, state)
        print(f"\nEstado guardado en {state_path.relative_to(repo_root)}")

    print("\nSincronizacion completada.")


if __name__ == "__main__":
    main()
