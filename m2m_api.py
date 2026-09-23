import math
from pathlib import Path

import pyodbc
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse


app = FastAPI()

STATIC_DIR = Path(__file__).resolve().parent / "static"


def get_connection():
    return pyodbc.connect("DSN=M2M_SQL;DATABASE=m2mdata01;")


def rows_to_dicts(description, rows) -> list[dict]:
    columns = [column[0] for column in description]
    results = []
    for row in rows:
        item = {}
        for column, value in zip(columns, row):
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            elif hasattr(value, "as_tuple"):
                value = float(value)
            item[column] = value
        results.append(item)
    return results


def _fetch_dicts(cursor, sql: str, *params) -> list[dict]:
    cursor.execute(sql, *params)
    return rows_to_dicts(cursor.description, cursor.fetchall())


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

PART_LABOR_HISTORY_SQL = """
SELECT
    LTRIM(RTRIM(jm.fpartno)) AS PartNumber,
    LTRIM(RTRIM(jm.fpartrev)) AS PartRevision,
    LTRIM(RTRIM(ld.fjobno)) AS JobNumber,
    ld.foperno AS OperationNumber,
    LTRIM(RTRIM(jr.fpro_id)) AS WorkCenter,
    wc.WorkCenterName AS WorkCenterName,
    LTRIM(RTRIM(jr.fopermemo)) AS OperationDescription,
    LTRIM(RTRIM(ld.fempno)) AS EmployeeNumber,
    LTRIM(RTRIM(ISNULL(pe.ffname, ''))) + ' ' +
    LTRIM(RTRIM(ISNULL(pe.fname, ''))) AS EmployeeName,
    CASE
        WHEN ld.fcode1 = 'S' THEN 'Setup'
        WHEN ld.fcode1 = 'P' THEN 'Production'
        ELSE ld.fcode1
    END AS LaborType,
    ld.fdate AS LaborDate,
    ld.fsdatetime AS StartDateTime,
    ld.fedatetime AS EndDateTime,
    ld.fcompqty AS QuantityComplete,
    ld.fscrpqty AS ScrapQuantity,
    DATEDIFF(SECOND, ld.fsdatetime, ld.fedatetime) / 3600.0 AS ActualHours,
    M2MReport.GetEstimat(ld.fjobno, ld.foperno, ld.fcode1, ld.fcompqty) AS EstimatedHours
FROM ladetail ld
JOIN jomast jm
    ON jm.fjobno = ld.fjobno
LEFT JOIN jodrtg jr
    ON jr.fjobno = ld.fjobno
    AND jr.foperno = ld.foperno
OUTER APPLY
(
    SELECT TOP 1
        LTRIM(RTRIM(v.fcpro_name)) AS WorkCenterName
    FROM vw_tot_direct_wc v
    WHERE
        LTRIM(RTRIM(v.fpro_id)) = LTRIM(RTRIM(jr.fpro_id))
        AND LTRIM(RTRIM(ISNULL(v.fcpro_name, ''))) <> ''
    ORDER BY v.fdate DESC
) wc
LEFT JOIN prempl pe
    ON pe.fempno = ld.fempno
WHERE jm.fpartno = ?
ORDER BY ld.foperno, ld.fdate, ld.fjobno, ld.fsdatetime
"""

LIVE_JOBS_SQL = """
WITH ActiveJobs AS
(
    SELECT
        ec.fempno,
        ec.fcjono,
        ec.foperno,
        ec.ftype,
        ec.fdatetime,
        ROW_NUMBER() OVER
        (
            PARTITION BY ec.fempno, ec.fcjono, ec.foperno
            ORDER BY ec.fdatetime DESC
        ) AS rn
    FROM EmpClckin ec
    WHERE
        LTRIM(RTRIM(ec.fempno)) = ?
        AND ec.flclockoff = 0
        AND ec.fdclockoff = '1900-01-01'
        AND LTRIM(RTRIM(ISNULL(ec.fcjono, ''))) <> ''
)
SELECT
    LTRIM(RTRIM(a.fempno)) AS EmployeeNumber,
    LTRIM(RTRIM(ISNULL(pe.ffname, ''))) + ' ' +
    LTRIM(RTRIM(ISNULL(pe.fname, ''))) AS EmployeeName,
    LTRIM(RTRIM(a.fcjono)) AS JobNumber,
    a.foperno AS OperationNumber,
    a.ftype AS ActiveTypeCode,
    CASE
        WHEN a.ftype = 'S' THEN 'Setup'
        ELSE 'Production'
    END AS ActiveLaborType,
    a.fdatetime AS ClockInTime,
    LTRIM(RTRIM(jm.fpartno)) AS PartNumber,
    LTRIM(RTRIM(jm.fpartrev)) AS Revision,
    LTRIM(RTRIM(jr.fpro_id)) AS WorkCenter,
    LTRIM(RTRIM(jr.fopermemo)) AS OperationDescription,
    jm.fquantity AS JobQuantity,
    jr.foperqty AS OperationQuantity,
    jr.fnqty_comp AS OperationQtyReported,
    jr.fnqty_togo AS OperationQtyRemaining,
    DATEDIFF(SECOND, a.fdatetime, GETDATE()) / 3600.0 AS CurrentSegmentHours,
    M2MReport.GetEstimat(
        a.fcjono,
        a.foperno,
        'P',
        jr.foperqty
    ) AS FullProductionEstimate,
    M2MReport.GetEstimat(
        a.fcjono,
        a.foperno,
        'P',
        jr.fnqty_togo
    ) AS RemainingProductionEstimate,
    M2MReport.GetEstimat(
        a.fcjono,
        a.foperno,
        'S',
        0
    ) AS SetupEstimate,
    CASE
        WHEN M2MReport.GetEstimat(
            a.fcjono,
            a.foperno,
            'P',
            jr.foperqty
        ) > 0
        THEN
            jr.foperqty /
            M2MReport.GetEstimat(
                a.fcjono,
                a.foperno,
                'P',
                jr.foperqty
            )
        ELSE 0
    END AS TargetPiecesPerHour
FROM ActiveJobs a
JOIN jomast jm
    ON jm.fjobno = a.fcjono
LEFT JOIN jodrtg jr
    ON jr.fjobno = a.fcjono
    AND jr.foperno = a.foperno
LEFT JOIN prempl pe
    ON pe.fempno = a.fempno
WHERE
    a.rn = 1
    AND LTRIM(RTRIM(jm.fpartno)) <> 'FAB INDIRECT LABOR'
ORDER BY a.fdatetime DESC, a.fcjono, a.foperno
"""

LIVE_DASHBOARD_SQL = """
WITH ActiveJobs AS
(
    SELECT
        ec.fempno,
        ec.fcjono,
        ec.foperno,
        ec.ftype,
        ec.fdatetime,
        ROW_NUMBER() OVER
        (
            PARTITION BY ec.fempno, ec.fcjono, ec.foperno
            ORDER BY ec.fdatetime DESC
        ) AS rn
    FROM EmpClckin ec
    WHERE
        ec.flclockoff = 0
        AND ec.fdclockoff = '1900-01-01'
        AND LTRIM(RTRIM(ISNULL(ec.fcjono, ''))) <> ''
)
SELECT
    LTRIM(RTRIM(a.fempno)) AS EmployeeNumber,
    LTRIM(RTRIM(ISNULL(pe.ffname, ''))) + ' ' +
    LTRIM(RTRIM(ISNULL(pe.fname, ''))) AS EmployeeName,
    LTRIM(RTRIM(a.fcjono)) AS JobNumber,
    a.foperno AS OperationNumber,
    a.ftype AS ActiveTypeCode,
    CASE
        WHEN a.ftype = 'S' THEN 'Setup'
        ELSE 'Production'
    END AS ActiveLaborType,
    a.fdatetime AS ClockInTime,
    LTRIM(RTRIM(jm.fpartno)) AS PartNumber,
    LTRIM(RTRIM(jm.fpartrev)) AS Revision,
    LTRIM(RTRIM(jr.fpro_id)) AS WorkCenter,
    wc.WorkCenterName AS WorkCenterName,
    LTRIM(RTRIM(jr.fopermemo)) AS OperationDescription,
    jr.foperqty AS OperationQuantity,
    jr.fnqty_comp AS OperationQtyReported,
    jr.fnqty_togo AS OperationQtyRemaining,
    DATEDIFF(SECOND, a.fdatetime, GETDATE()) / 3600.0 AS CurrentSegmentHours
FROM ActiveJobs a
JOIN jomast jm
    ON jm.fjobno = a.fcjono
LEFT JOIN jodrtg jr
    ON jr.fjobno = a.fcjono
    AND jr.foperno = a.foperno
OUTER APPLY
(
    SELECT TOP 1
        LTRIM(RTRIM(v.fcpro_name)) AS WorkCenterName
    FROM vw_tot_direct_wc v
    WHERE
        LTRIM(RTRIM(v.fpro_id)) = LTRIM(RTRIM(jr.fpro_id))
        AND LTRIM(RTRIM(ISNULL(v.fcpro_name, ''))) <> ''
    ORDER BY v.fdate DESC
) wc
LEFT JOIN prempl pe
    ON pe.fempno = a.fempno
WHERE
    a.rn = 1
    AND LTRIM(RTRIM(jm.fpartno)) <> 'FAB INDIRECT LABOR'
ORDER BY
    ISNULL(wc.WorkCenterName, jr.fpro_id),
    pe.fname,
    pe.ffname,
    a.fcjono,
    a.foperno
"""

# Labor history for every part/revision/operation someone is currently clocked onto.
# {employee_filter} narrows the active keys to one employee.
ACTIVE_KEYS_HISTORY_SQL = """
WITH ActiveKeys AS
(
    SELECT DISTINCT
        LTRIM(RTRIM(jm.fpartno)) AS PartNumber,
        LTRIM(RTRIM(jm.fpartrev)) AS PartRevision,
        ec.foperno AS OperationNumber
    FROM EmpClckin ec
    JOIN jomast jm
        ON jm.fjobno = ec.fcjono
    WHERE
        {employee_filter}ec.flclockoff = 0
        AND ec.fdclockoff = '1900-01-01'
        AND LTRIM(RTRIM(ISNULL(ec.fcjono, ''))) <> ''
        AND LTRIM(RTRIM(jm.fpartno)) <> 'FAB INDIRECT LABOR'
)
SELECT
    LTRIM(RTRIM(jm.fpartno)) AS PartNumber,
    LTRIM(RTRIM(jm.fpartrev)) AS PartRevision,
    LTRIM(RTRIM(ld.fjobno)) AS JobNumber,
    ld.foperno AS OperationNumber,
    LTRIM(RTRIM(ld.fempno)) AS EmployeeNumber,
    CASE
        WHEN ld.fcode1 = 'S' THEN 'Setup'
        WHEN ld.fcode1 = 'P' THEN 'Production'
        ELSE ld.fcode1
    END AS LaborType,
    ld.fdate AS LaborDate,
    ld.fcompqty AS QuantityComplete,
    DATEDIFF(SECOND, ld.fsdatetime, ld.fedatetime) / 3600.0 AS ActualHours,
    M2MReport.GetEstimat(ld.fjobno, ld.foperno, ld.fcode1, ld.fcompqty) AS EstimatedHours
FROM ladetail ld
JOIN jomast jm
    ON jm.fjobno = ld.fjobno
JOIN ActiveKeys ak
    ON ak.PartNumber = LTRIM(RTRIM(jm.fpartno))
    AND ak.PartRevision = LTRIM(RTRIM(jm.fpartrev))
    AND ak.OperationNumber = ld.foperno
WHERE ld.fcode1 IN ('S', 'P')
ORDER BY jm.fpartno, jm.fpartrev, ld.foperno, ld.fdate, ld.fjobno
"""

# The two variants differ only in the employee predicate.
LIVE_HISTORY_SQL = ACTIVE_KEYS_HISTORY_SQL.format(employee_filter="LTRIM(RTRIM(ec.fempno)) = ?\n        AND ")
DASHBOARD_HISTORY_SQL = ACTIVE_KEYS_HISTORY_SQL.format(employee_filter="")


ESTIMATE_SQL = """
SELECT
    M2MReport.GetEstimat(?, ?, 'P', ?) AS EarnedHours
"""


# ---------------------------------------------------------------------------
# History statistics
# ---------------------------------------------------------------------------

def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _combine_dashboard_history(rows: list[dict]) -> list[dict]:
    groups = {}
    for row in rows:
        labor_date = row.get("LaborDate") or ""
        if isinstance(labor_date, str):
            labor_date = labor_date[:10]
        key = (
            row.get("JobNumber"),
            row.get("OperationNumber"),
            row.get("EmployeeNumber"),
            labor_date,
            row.get("LaborType"),
            row.get("PartRevision"),
        )
        if key not in groups:
            groups[key] = {
                "PartNumber": row.get("PartNumber"),
                "JobNumber": row.get("JobNumber"),
                "OperationNumber": row.get("OperationNumber"),
                "EmployeeNumber": row.get("EmployeeNumber"),
                "LaborDate": labor_date,
                "LaborType": row.get("LaborType"),
                "PartRevision": row.get("PartRevision"),
                "QuantityComplete": 0.0,
                "ActualHours": 0.0,
                "EstimatedHours": 0.0,
            }
        group = groups[key]
        group["QuantityComplete"] += _safe_float(row.get("QuantityComplete"))
        group["ActualHours"] += _safe_float(row.get("ActualHours"))
        group["EstimatedHours"] += _safe_float(row.get("EstimatedHours"))

    combined = list(groups.values())
    for row in combined:
        row["VarianceHours"] = row["ActualHours"] - row["EstimatedHours"]
    return combined


def _dashboard_history_summary(rows: list[dict], labor_type: str) -> dict:
    filtered = [row for row in rows if row.get("LaborType") == labor_type]
    if labor_type == "Production":
        filtered = [row for row in filtered if _safe_float(row.get("QuantityComplete")) > 0]

    if not filtered:
        return {
            "Runs": 0,
            "Jobs": 0,
            "AdjustedRuns": 0,
            "AvgQuantity": 0.0,
            "AvgActualHours": 0.0,
            "AvgEstimatedHours": 0.0,
            "AvgVarianceHours": 0.0,
            "WeightedEfficiency": 0.0,
        }

    adjusted = filtered
    if len(filtered) > 2:
        variances = [_safe_float(row.get("VarianceHours")) for row in filtered]
        mean = sum(variances) / len(variances)
        variance = sum((value - mean) ** 2 for value in variances) / len(variances)
        sd = math.sqrt(variance)
        if sd > 0:
            lower = mean - sd
            upper = mean + sd
            adjusted = [
                row for row in filtered
                if lower <= _safe_float(row.get("VarianceHours")) <= upper
            ]
            if not adjusted:
                adjusted = filtered

    count = len(adjusted)
    total_actual = sum(_safe_float(row.get("ActualHours")) for row in adjusted)
    total_estimated = sum(_safe_float(row.get("EstimatedHours")) for row in adjusted)
    total_qty = sum(_safe_float(row.get("QuantityComplete")) for row in adjusted)
    total_variance = sum(_safe_float(row.get("VarianceHours")) for row in adjusted)

    return {
        "Runs": len(filtered),
        "Jobs": len({row.get("JobNumber") for row in filtered if row.get("JobNumber")}),
        "AdjustedRuns": count,
        "AvgQuantity": total_qty / count if count else 0.0,
        "AvgActualHours": total_actual / count if count else 0.0,
        "AvgEstimatedHours": total_estimated / count if count else 0.0,
        "AvgVarianceHours": total_variance / count if count else 0.0,
        "WeightedEfficiency": (total_estimated / total_actual * 100.0) if total_actual > 0 else 0.0,
    }


def _history_key(row: dict, revision_field: str) -> tuple[str, str, int]:
    return (
        str(row.get("PartNumber") or "").strip(),
        str(row.get(revision_field) or "").strip(),
        int(row.get("OperationNumber") or 0),
    )


def _attach_history_summaries(jobs: list[dict], history: list[dict]) -> None:
    """Add HistoricalSetup / HistoricalProduction summaries to each active job, in place."""
    history_by_key = {}
    for row in _combine_dashboard_history(history):
        history_by_key.setdefault(_history_key(row, "PartRevision"), []).append(row)

    for job in jobs:
        key_rows = history_by_key.get(_history_key(job, "Revision"), [])
        job["HistoricalSetup"] = _dashboard_history_summary(key_rows, "Setup")
        job["HistoricalProduction"] = _dashboard_history_summary(key_rows, "Production")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/part-labor-history/{part_number}")
def get_part_labor_history(part_number: str):
    conn = get_connection()
    cursor = conn.cursor()

    try:
        results = _fetch_dicts(cursor, PART_LABOR_HISTORY_SQL, part_number)
    finally:
        conn.close()

    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"No labor history found for part {part_number}"
        )

    return results


@app.get("/api/live/{employee_number}")
def get_live_employee(employee_number: str):
    conn = get_connection()
    cursor = conn.cursor()

    history = []
    try:
        results = _fetch_dicts(cursor, LIVE_JOBS_SQL, employee_number)
        if results:
            history = _fetch_dicts(cursor, LIVE_HISTORY_SQL, employee_number)
    finally:
        conn.close()

    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"No active direct labor job found for employee {employee_number}"
        )

    _attach_history_summaries(results, history)
    return results


@app.get("/api/live-estimate")
def get_live_estimate(
    job_number: str = Query(...),
    operation_number: int = Query(...),
    quantity: float = Query(..., ge=0)
):
    conn = get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(ESTIMATE_SQL, job_number, operation_number, quantity)
        row = cursor.fetchone()
        if row is None:
            earned = 0.0
        else:
            earned = float(row[0] or 0)
    finally:
        conn.close()

    return {
        "JobNumber": job_number,
        "OperationNumber": operation_number,
        "Quantity": quantity,
        "EarnedHours": earned
    }


@app.get("/api/live-dashboard")
def get_live_dashboard():
    conn = get_connection()
    cursor = conn.cursor()

    try:
        active_jobs = _fetch_dicts(cursor, LIVE_DASHBOARD_SQL)
        if not active_jobs:
            return {"jobs": [], "count": 0}
        history = _fetch_dicts(cursor, DASHBOARD_HISTORY_SQL)
    finally:
        conn.close()

    _attach_history_summaries(active_jobs, history)
    return {
        "jobs": active_jobs,
        "count": len(active_jobs),
    }


# ---------------------------------------------------------------------------
# Pages (HTML lives in static/; read once at startup, as the inline strings were)
# ---------------------------------------------------------------------------

HISTORY_HTML = (STATIC_DIR / "history.html").read_text(encoding="utf-8")
LIVE_HTML = (STATIC_DIR / "live.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def home():
    return HISTORY_HTML


@app.get("/live", response_class=HTMLResponse)
def live_page():
    return LIVE_HTML


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return DASHBOARD_HTML
