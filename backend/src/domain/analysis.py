from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import re


TEAMS = ("压线甲班", "干燥乙班", "整理丙班")
CAUSES = ("干燥开裂", "直径偏差", "断条", "表面毛刺", "长短不齐")
CAUSE_ORDER = {cause: index for index, cause in enumerate(CAUSES)}
DEFAULT_TARGET = 80
COLUMNS = (
    ("kind", "结果类型"),
    ("team", "班组"),
    ("cause", "返工原因"),
    ("batch_code", "返工批次"),
    ("rework_date", "返工日期"),
    ("weight", "归因权重"),
    ("minutes", "返工工时（人分钟）"),
    ("labor_cents", "人工损失（分）"),
    ("material_cents", "物料损失（分）"),
    ("total_cents", "合计损失（分）"),
    ("share_pct", "班组贡献率（%）"),
    ("cumulative_pct", "班组累计贡献率（%）"),
    ("priority", "整改优先级"),
    ("note", "复核备注"),
)


@dataclass(frozen=True)
class Loss:
    minutes: int = 0
    labor_cents: int = 0
    material_cents: int = 0

    def __add__(self, other):
        return Loss(self.minutes + other.minutes, self.labor_cents + other.labor_cents,
                    self.material_cents + other.material_cents)

    @property
    def total_cents(self):
        return self.labor_cents + self.material_cents

    def as_cells(self):
        return {"minutes": self.minutes, "labor_cents": self.labor_cents,
                "material_cents": self.material_cents, "total_cents": self.total_cents}


def _integer(value, label, minimum, maximum):
    if type(value) not in (int, float) or not minimum <= value <= maximum or value % 1:
        raise ValueError(f"{label}须为 {minimum} 至 {maximum} 的整数")
    return int(value)


def _parse_weights(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写返工原因与正整数权重，例如 干燥开裂=3,断条=1")
    weights = {}
    for entry in value.replace("，", ",").split(","):
        cause, _, weight = (part.strip() for part in entry.partition("="))
        if cause not in CAUSE_ORDER:
            raise ValueError(f"未知返工原因：{cause or '空白'}。请使用：{'、'.join(CAUSES)}")
        if cause in weights:
            raise ValueError(f"返工原因 {cause} 重复，请合并为一个权重")
        if not re.fullmatch(r"[1-9][0-9]{0,3}", weight) or int(weight) > 1000:
            raise ValueError(f"{cause} 的权重须为 1 至 1000 的整数，格式为 原因=权重")
        weights[cause] = int(weight)
    return weights


def _half_up(numerator, denominator):
    whole, remainder = divmod(numerator, denominator)
    return whole + (2 * remainder >= denominator)


def _read_batch(row):
    code = row.get("batch_code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("返工批次不能为空，且须为文本")
    team = row.get("team")
    if team not in TEAMS:
        raise ValueError(f"班组须为：{'、'.join(TEAMS)}")
    raw_date = row.get("rework_date")
    try:
        rework_date = date.fromisoformat(raw_date)
    except (TypeError, ValueError):
        raise ValueError("返工日期须为有效的 YYYY-MM-DD") from None
    if rework_date.isoformat() != raw_date:
        raise ValueError("返工日期须为有效的 YYYY-MM-DD")
    minutes = _integer(row.get("labor_minutes"), "返工工时（人分钟）", 0, 10080)
    hourly_rate = _integer(row.get("hourly_labor_cents"), "人工费率（分/人小时）", 1, 100000)
    material = _integer(row.get("material_cents"), "物料损失（分）", 0, 10000000)
    weights = _parse_weights(row.get("cause_weights"))
    note = row.get("review_note")
    if note is not None and not isinstance(note, str):
        raise ValueError("复核备注须为文本")
    return code.strip(), team, raw_date, Loss(minutes, _half_up(minutes * hourly_rate, 60), material), weights, note


def validate(row: dict) -> None:
    _read_batch(row)


def _allocate(total, weights):
    """Allocate whole units by largest remainder, with a fixed cause order for ties."""
    denominator = sum(weights.values())
    quotas = {cause: divmod(total * weight, denominator) for cause, weight in weights.items()}
    allocated = {cause: whole for cause, (whole, _) in quotas.items()}
    left = total - sum(allocated.values())
    order = sorted(quotas, key=lambda cause: (-quotas[cause][1], CAUSE_ORDER[cause]))
    for cause in order[:left]:
        allocated[cause] += 1
    return allocated


def _share(amount, total):
    return _half_up(10000 * amount, total) / 100 if total else None


def analyze(rows: list[dict], parameters: dict) -> dict:
    target = _integer(parameters.get("priority_coverage_pct", DEFAULT_TARGET), "班组优先整改覆盖率（%）", 1, 100)
    if not rows:
        raise ValueError("请至少录入一条已复核的返工批次")
    batches = sorted(map(_read_batch, rows), key=lambda item: (item[2], item[0]))
    batch_codes = set()
    source_total = Loss()
    team_totals = defaultdict(Loss)
    team_causes = defaultdict(lambda: defaultdict(Loss))
    cause_totals = defaultdict(Loss)
    batch_rows = []
    for code, team, rework_date, loss, weights, note in batches:
        if code in batch_codes:
            raise ValueError(f"返工批次 {code} 重复，请保留一条复核记录")
        batch_codes.add(code)
        source_total += loss
        team_totals[team] += loss
        minutes = _allocate(loss.minutes, weights)
        labor = _allocate(loss.labor_cents, weights)
        material = _allocate(loss.material_cents, weights)
        context = {"team": team, "batch_code": code, "rework_date": rework_date}
        batch_rows.append({"kind": "批次账面", **context, **loss.as_cells(), "note": note})
        for cause in sorted(weights, key=CAUSE_ORDER.__getitem__):
            allocated = Loss(minutes[cause], labor[cause], material[cause])
            team_causes[team][cause] += allocated
            cause_totals[cause] += allocated
            batch_rows.append({"kind": "批次分摊", **context, "cause": cause,
                               "weight": weights[cause], **allocated.as_cells()})

    overview = []
    first_priority = None
    for team in sorted(team_totals, key=lambda name: (-team_totals[name].total_cents, name)):
        team_loss = team_totals[team]
        overview.append({"kind": "班组小计", "team": team, **team_loss.as_cells()})
        ordered = sorted(team_causes[team].items(), key=lambda item: (-item[1].total_cents, CAUSE_ORDER[item[0]]))
        captured = 0
        for cause, loss in ordered:
            # Compare exact amounts before rounding the displayed percentage.
            priority = loss.total_cents > 0 and 100 * captured < target * team_loss.total_cents
            captured += loss.total_cents
            if priority and first_priority is None:
                first_priority = team, cause, loss.total_cents
            overview.append({
                "kind": "班组拆因", "team": team, "cause": cause, **loss.as_cells(),
                "share_pct": _share(loss.total_cents, team_loss.total_cents),
                "cumulative_pct": _share(captured, team_loss.total_cents),
                "priority": "优先整改" if priority else "—",
            })

    allocated_total = sum(cause_totals.values(), start=Loss())
    reconciliation = allocated_total.total_cents - source_total.total_cents
    if first_priority:
        team, cause, amount = first_priority
        recommendation = f"建议{team}先复核{cause}，该项分摊损失为 {amount} 分。"
    else:
        recommendation = "本批账面损失为 0 分，按数量与现场记录复核返工。"
    return {
        "headline": "线香返工损失拆因与对账",
        "summary": (
            f"各班组从损失最高的原因开始选入整改清单，直至累计覆盖至少 {target}%。"
            f"{recommendation}归因权重取自复核记录，整改前请复核批次证据。"
        ),
        "metrics": [
            {"label": "返工批次", "value": len(batches), "unit": "批"},
            {"label": "账面损失", "value": source_total.total_cents, "unit": "分"},
            {"label": "分摊损失", "value": allocated_total.total_cents, "unit": "分"},
            {"label": "对账差额（分摊减账面）", "value": reconciliation, "unit": "分"},
        ],
        "columns": [{"key": key, "label": label} for key, label in COLUMNS],
        "rows": [{key: row.get(key) for key, _ in COLUMNS} for row in overview + batch_rows],
        "chart": {"label": "各原因返工损失合计", "unit": "分", "points": [
            {"label": cause, "value": loss.total_cents}
            for cause, loss in sorted(cause_totals.items(), key=lambda item: (-item[1].total_cents, CAUSE_ORDER[item[0]]))
        ]},
    }


def seed() -> list[dict]:
    profiles = (
        ("压线甲班", "YX", 85, 4200, 5150, ("直径偏差=5,断条=2", "直径偏差=3,表面毛刺=2", "直径偏差=4,长短不齐=1")),
        ("干燥乙班", "GZ", 132, 4513, 10900, ("干燥开裂=5,断条=2", "干燥开裂=4,直径偏差=1", "干燥开裂=6,表面毛刺=1")),
        ("整理丙班", "ZL", 51, 3875, 2950, ("长短不齐=5,断条=2", "表面毛刺=3,长短不齐=2", "长短不齐=3,直径偏差=1")),
    )
    rows = []
    for team, prefix, base_minutes, rate, base_material, options in profiles:
        for index in range(14):
            rework_date = date(2026, 11, 9) + timedelta(days=index // 2)
            row = {
                "batch_code": f"{prefix}-2611-{index + 1:02d}", "team": team, "rework_date": rework_date.isoformat(),
                "labor_minutes": base_minutes + index * 13 % 47, "hourly_labor_cents": rate,
                "material_cents": base_material + index * 593 % 2800, "cause_weights": options[index % len(options)],
            }
            if prefix == "GZ" and index == 0:
                row.update(labor_minutes=90, material_cents=10001, cause_weights="干燥开裂=1,断条=1",
                           review_note="首批同权归因，核对复称与工时签单")
            rows.append(row)
    return rows
