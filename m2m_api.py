from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
import pyodbc
from datetime import date, timedelta


app = FastAPI()



def get_connection():
    return pyodbc.connect("DSN=M2M_SQL;DATABASE=m2mdata01;Trusted_Connection=true")


def rows_to_dicts(cursor, rows):
    columns = [column[0] for column in cursor.description]
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


@app.get("/api/part-labor-history/{part_number}")
def get_part_labor_history(part_number: str):
    conn = get_connection()
    cursor = conn.cursor()

    sql = """
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
        LTRIM(RTRIM(iw.fcpro_name)) AS WorkCenterName
    FROM inwork iw
    WHERE
        LTRIM(RTRIM(iw.fcpro_id)) = LTRIM(RTRIM(jr.fpro_id))
        AND LTRIM(RTRIM(ISNULL(iw.fcpro_name, ''))) <> ''
    ORDER BY LTRIM(RTRIM(iw.fcpro_name))
) wc
LEFT JOIN prempl pe
    ON pe.fempno = ld.fempno
WHERE jm.fpartno = ?
ORDER BY ld.foperno, ld.fdate, ld.fjobno, ld.fsdatetime
"""

    try:
        cursor.execute(sql, part_number)
        rows = cursor.fetchall()
        results = rows_to_dicts(cursor, rows)
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

    sql = """
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

    history_sql = """
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
        LTRIM(RTRIM(ec.fempno)) = ?
        AND ec.flclockoff = 0
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

    history = []
    try:
        cursor.execute(sql, employee_number)
        rows = cursor.fetchall()
        results = rows_to_dicts(cursor, rows)

        if results:
            cursor.execute(history_sql, employee_number)
            history_rows = cursor.fetchall()
            history = rows_to_dicts(cursor, history_rows)
    finally:
        conn.close()

    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"No active direct labor job found for employee {employee_number}"
        )

    combined_history = _combine_dashboard_history(history)
    history_by_key = {}
    for row in combined_history:
        key = (
            str(row.get("PartNumber") or "").strip(),
            str(row.get("PartRevision") or "").strip(),
            int(row.get("OperationNumber") or 0),
        )
        history_by_key.setdefault(key, []).append(row)

    for job in results:
        key = (
            str(job.get("PartNumber") or "").strip(),
            str(job.get("Revision") or "").strip(),
            int(job.get("OperationNumber") or 0),
        )
        key_rows = history_by_key.get(key, [])
        job["HistoricalSetup"] = _dashboard_history_summary(key_rows, "Setup")
        job["HistoricalProduction"] = _dashboard_history_summary(key_rows, "Production")

    return results


@app.get("/api/live-estimate")
def get_live_estimate(
    job_number: str = Query(...),
    operation_number: int = Query(...),
    quantity: float = Query(..., ge=0)
):
    conn = get_connection()
    cursor = conn.cursor()

    sql = """
SELECT
    M2MReport.GetEstimat(?, ?, 'P', ?) AS EarnedHours
"""

    try:
        cursor.execute(sql, job_number, operation_number, quantity)
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



def _safe_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _combine_dashboard_history(rows):
    groups = {}
    for row in rows:
        labor_date = row.get("LaborDate") or ""
        if isinstance(labor_date, str):
            labor_date = labor_date[:10]

        labor_type = row.get("LaborType")
        is_setup = labor_type == "Setup"

        # Production keeps the existing employee/date grouping.
        # Setup is one historical comparison per Job + Operation.
        if is_setup:
            key = (
                row.get("JobNumber"),
                row.get("OperationNumber"),
                labor_type,
                row.get("PartRevision"),
            )
        else:
            key = (
                row.get("JobNumber"),
                row.get("OperationNumber"),
                row.get("EmployeeNumber"),
                labor_date,
                labor_type,
                row.get("PartRevision"),
            )

        if key not in groups:
            groups[key] = {
                "PartNumber": row.get("PartNumber"),
                "JobNumber": row.get("JobNumber"),
                "OperationNumber": row.get("OperationNumber"),
                "EmployeeNumber": row.get("EmployeeNumber"),
                "LaborDate": labor_date,
                "LaborType": labor_type,
                "PartRevision": row.get("PartRevision"),
                "QuantityComplete": 0.0,
                "ActualHours": 0.0,
                "EstimatedHours": 0.0,
                "_SetupEstimateCaptured": False,
            }

        group = groups[key]
        group["QuantityComplete"] += _safe_float(row.get("QuantityComplete"))
        group["ActualHours"] += _safe_float(row.get("ActualHours"))

        estimated = _safe_float(row.get("EstimatedHours"))
        if is_setup:
            # M2M can repeat the same setup estimate on every setup labor entry.
            # Use the setup estimate once for the whole job/operation.
            if not group["_SetupEstimateCaptured"] and estimated > 0:
                group["EstimatedHours"] = estimated
                group["_SetupEstimateCaptured"] = True
        else:
            group["EstimatedHours"] += estimated

        if labor_date and (not group["LaborDate"] or labor_date < group["LaborDate"]):
            group["LaborDate"] = labor_date

    combined = list(groups.values())
    for row in combined:
        row.pop("_SetupEstimateCaptured", None)
        row["VarianceHours"] = row["ActualHours"] - row["EstimatedHours"]

    return combined


def _median(values):
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 0.0
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _dashboard_history_summary(rows, labor_type):
    filtered = [row for row in rows if row.get("LaborType") == labor_type]
    if labor_type == "Production":
        filtered = [row for row in filtered if _safe_float(row.get("QuantityComplete")) > 0]

    if not filtered:
        return {
            "Runs": 0,
            "Jobs": 0,
            "AdjustedRuns": 0,
            "ExcludedRuns": 0,
            "Below80Runs": 0,
            "HighReviewRuns": 0,
            "StatisticalOutliers": 0,
            "DataReviewRuns": 0,
            "AvgQuantity": 0.0,
            "AvgActualHours": 0.0,
            "AvgEstimatedHours": 0.0,
            "AvgVarianceHours": 0.0,
            "WeightedEfficiency": 0.0,
            "RawWeightedEfficiency": 0.0,
        }

    valid_ratio_rows = []
    ratios = []
    for row in filtered:
        actual = _safe_float(row.get("ActualHours"))
        estimated = _safe_float(row.get("EstimatedHours"))
        if actual > 0 and estimated > 0:
            ratio = actual / estimated
            valid_ratio_rows.append((row, ratio))
            ratios.append(ratio)

    median_ratio = _median(ratios) if ratios else 0.0
    deviations = [abs(value - median_ratio) for value in ratios]
    mad = _median(deviations) if deviations else 0.0
    use_mad = len(ratios) >= 5 and mad > 0

    adjusted = []
    below_80 = 0
    high_review = 0
    statistical_outliers = 0
    data_review = 0

    for row in filtered:
        actual = _safe_float(row.get("ActualHours"))
        estimated = _safe_float(row.get("EstimatedHours"))

        if actual <= 0 or estimated <= 0:
            data_review += 1
            continue

        ratio = actual / estimated
        modified_z = (0.6745 * (ratio - median_ratio) / mad) if use_mad else 0.0

        if use_mad and abs(modified_z) > 3.5:
            statistical_outliers += 1
            continue

        efficiency = (estimated / actual) * 100.0
        if efficiency < 80.0:
            below_80 += 1
        elif ratio <= 0.80:  # at least 20% faster than estimate => >=125% efficiency
            high_review += 1

        adjusted.append(row)

    raw_actual = sum(_safe_float(row.get("ActualHours")) for row in filtered)
    raw_estimated = sum(_safe_float(row.get("EstimatedHours")) for row in filtered)

    count = len(adjusted)
    total_actual = sum(_safe_float(row.get("ActualHours")) for row in adjusted)
    total_estimated = sum(_safe_float(row.get("EstimatedHours")) for row in adjusted)
    total_qty = sum(_safe_float(row.get("QuantityComplete")) for row in adjusted)
    total_variance = sum(_safe_float(row.get("VarianceHours")) for row in adjusted)

    return {
        "Runs": len(filtered),
        "Jobs": len({row.get("JobNumber") for row in filtered if row.get("JobNumber")}),
        "AdjustedRuns": count,
        "ExcludedRuns": len(filtered) - count,
        "Below80Runs": below_80,
        "HighReviewRuns": high_review,
        "StatisticalOutliers": statistical_outliers,
        "DataReviewRuns": data_review,
        "AvgQuantity": total_qty / count if count else 0.0,
        "AvgActualHours": total_actual / count if count else 0.0,
        "AvgEstimatedHours": total_estimated / count if count else 0.0,
        "AvgVarianceHours": total_variance / count if count else 0.0,
        "WeightedEfficiency": (total_estimated / total_actual * 100.0) if total_actual > 0 else 0.0,
        "RawWeightedEfficiency": (raw_estimated / raw_actual * 100.0) if raw_actual > 0 else 0.0,
    }


@app.get("/api/live-dashboard")
def get_live_dashboard():
    conn = get_connection()
    cursor = conn.cursor()

    active_sql = """
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

    history_sql = """
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
        ec.flclockoff = 0
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

    try:
        cursor.execute(active_sql)
        active_rows = cursor.fetchall()
        active_jobs = rows_to_dicts(cursor, active_rows)

        if not active_jobs:
            return {"jobs": [], "count": 0}

        cursor.execute(history_sql)
        history_rows = cursor.fetchall()
        history = rows_to_dicts(cursor, history_rows)
    finally:
        conn.close()

    combined_history = _combine_dashboard_history(history)
    history_by_key = {}
    for row in combined_history:
        key = (
            str(row.get("PartNumber") or "").strip(),
            str(row.get("PartRevision") or "").strip(),
            int(row.get("OperationNumber") or 0),
        )
        history_by_key.setdefault(key, []).append(row)

    for job in active_jobs:
        key = (
            str(job.get("PartNumber") or "").strip(),
            str(job.get("Revision") or "").strip(),
            int(job.get("OperationNumber") or 0),
        )
        key_rows = history_by_key.get(key, [])
        job["HistoricalSetup"] = _dashboard_history_summary(key_rows, "Setup")
        job["HistoricalProduction"] = _dashboard_history_summary(key_rows, "Production")

    return {
        "jobs": active_jobs,
        "count": len(active_jobs),
    }


@app.get("/api/completed-job-review")
def get_completed_job_review(
    start_date: str = Query(""),
    end_date: str = Query("")
):
    """Return completed jobs plus operation-level labor performance for a date range.

    The date range selects jobs by the latest routing completion date.  Once a
    job is selected, all historical Setup and Production labor for that job is
    included in the performance calculation.
    """
    start_text = (start_date or "").strip()
    end_text = (end_date or "").strip()

    # Default to the previous Monday-Sunday week when no range is supplied.
    if not start_text and not end_text:
        today = date.today()
        this_monday = today - timedelta(days=today.weekday())
        end_value = this_monday - timedelta(days=1)
        start_value = end_value - timedelta(days=6)
    elif not start_text or not end_text:
        raise HTTPException(status_code=400, detail="Both start_date and end_date are required.")
    else:
        try:
            start_value = date.fromisoformat(start_text)
            end_value = date.fromisoformat(end_text)
        except ValueError:
            raise HTTPException(status_code=400, detail="Dates must use YYYY-MM-DD format.")

    if start_value > end_value:
        raise HTTPException(status_code=400, detail="start_date cannot be after end_date.")

    conn = get_connection()
    # This is a read-only reporting request against the live M2M database.
    # Autocommit + READ UNCOMMITTED keeps the report from holding a long read transaction.
    conn.autocommit = True
    cursor = conn.cursor()

    build_sql = """
SET NOCOUNT ON;
SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

DECLARE @StartDate DATE = ?;
DECLARE @EndDate DATE = ?;

IF OBJECT_ID('tempdb..#SelectedJobs') IS NOT NULL DROP TABLE #SelectedJobs;
IF OBJECT_ID('tempdb..#LaborAgg') IS NOT NULL DROP TABLE #LaborAgg;
IF OBJECT_ID('tempdb..#LaborCalc') IS NOT NULL DROP TABLE #LaborCalc;
IF OBJECT_ID('tempdb..#LaborCalcTagged') IS NOT NULL DROP TABLE #LaborCalcTagged;
IF OBJECT_ID('tempdb..#JobTotals') IS NOT NULL DROP TABLE #JobTotals;

WITH JobCompletion AS
(
    SELECT
        jm.fjobno AS JobNumberRaw,
        LTRIM(RTRIM(jm.fjobno)) AS JobNumber,
        LTRIM(RTRIM(jm.fpartno)) AS PartNumber,
        LTRIM(RTRIM(ISNULL(jm.fpartrev, ''))) AS PartRevision,
        LTRIM(RTRIM(jm.fstatus)) AS JobStatus,
        MAX(
            CASE
                WHEN rt.fcomp_date > '19000101' THEN rt.fcomp_date
                ELSE NULL
            END
        ) AS CompletionDate,
        COUNT(*) AS OperationCount,
        SUM(
            CASE
                WHEN rt.fnqty_togo > 0
                  OR rt.fcomp_date IS NULL
                  OR rt.fcomp_date <= '19000101'
                THEN 1 ELSE 0
            END
        ) AS IncompleteOperations
    FROM jomast jm
    INNER JOIN jodrtg rt
        ON rt.fjobno = jm.fjobno
    WHERE LTRIM(RTRIM(jm.fstatus)) IN ('COMPLETED', 'CLOSED')
    GROUP BY
        jm.fjobno,
        jm.fpartno,
        jm.fpartrev,
        jm.fstatus
)
SELECT
    JobNumberRaw,
    JobNumber,
    PartNumber,
    PartRevision,
    JobStatus,
    CompletionDate,
    OperationCount
INTO #SelectedJobs
FROM JobCompletion
WHERE IncompleteOperations = 0
  AND CompletionDate >= @StartDate
  AND CompletionDate < DATEADD(DAY, 1, @EndDate);

CREATE CLUSTERED INDEX IX_SelectedJobs_JobNumber
    ON #SelectedJobs(JobNumberRaw);

/* Aggregate labor before calling GetEstimat so the estimate function runs once
   per Job + Operation + Labor Type instead of once per labor transaction. */
SELECT
    ld.fjobno AS JobNumberRaw,
    ld.foperno AS OperationNumber,
    LTRIM(RTRIM(ld.fcode1)) AS LaborType,
    SUM(DATEDIFF(SECOND, ld.fsdatetime, ld.fedatetime) / 3600.0) AS ActualHours,
    SUM(ISNULL(ld.fcompqty, 0)) AS CompletedQty,
    MAX(ISNULL(ld.fcompqty, 0)) AS MaxCompletedQty
INTO #LaborAgg
FROM ladetail ld
INNER JOIN #SelectedJobs sj
    ON ld.fjobno = sj.JobNumberRaw
WHERE ld.fcode1 IN ('S', 'P')
  AND ld.fedatetime > ld.fsdatetime
GROUP BY
    ld.fjobno,
    ld.foperno,
    ld.fcode1;

CREATE CLUSTERED INDEX IX_LaborAgg
    ON #LaborAgg(JobNumberRaw, OperationNumber, LaborType);

/* Setup estimate is counted once only when setup labor actually exists.
   Production estimate is earned from the aggregated completed quantity. */
SELECT
    JobNumberRaw,
    OperationNumber,
    LaborType,
    ActualHours,
    CompletedQty,
    CASE
        WHEN LaborType = 'S'
        THEN ISNULL(M2MReport.GetEstimat(
            JobNumberRaw,
            OperationNumber,
            'S',
            MaxCompletedQty
        ), 0)
        WHEN LaborType = 'P'
        THEN ISNULL(M2MReport.GetEstimat(
            JobNumberRaw,
            OperationNumber,
            'P',
            CompletedQty
        ), 0)
        ELSE 0
    END AS EstimatedHours
INTO #LaborCalc
FROM #LaborAgg;

/* Tag designated Inspection / Shipping work centers so their labor variance does
   not affect Completed Job Review efficiency. Exclusion is based on work-center
   ID only; operation descriptions/memos do not affect this decision. */
SELECT
    lc.JobNumberRaw,
    lc.OperationNumber,
    lc.LaborType,
    lc.ActualHours,
    lc.CompletedQty,
    lc.EstimatedHours,
    LTRIM(RTRIM(ISNULL(rt.fpro_id, ''))) AS WorkCenter,
    ISNULL(wc.WorkCenterName, '') AS WorkCenterName,
    LTRIM(RTRIM(ISNULL(rt.fopermemo, ''))) AS OperationDescription,
    rt.fcomp_date AS OperationCompletionDate,
    CASE
        /* Exclusion is based ONLY on the routing work-center ID.
           Do not inspect the operation memo/description because normal
           manufacturing operations can contain words such as INSPECTION. */
        WHEN UPPER(LTRIM(RTRIM(ISNULL(rt.fpro_id, '')))) IN
             ('90', '91', '92', '95', '99', 'INSP99', 'N90', 'N91')
        THEN 1 ELSE 0
    END AS IgnoreVariance
INTO #LaborCalcTagged
FROM #LaborCalc lc
LEFT JOIN jodrtg rt
    ON rt.fjobno = lc.JobNumberRaw
    AND rt.foperno = lc.OperationNumber
OUTER APPLY
(
    SELECT TOP 1
        LTRIM(RTRIM(iw.fcpro_name)) AS WorkCenterName
    FROM inwork iw
    WHERE
        LTRIM(RTRIM(iw.fcpro_id)) = LTRIM(RTRIM(rt.fpro_id))
        AND LTRIM(RTRIM(ISNULL(iw.fcpro_name, ''))) <> ''
    ORDER BY LTRIM(RTRIM(iw.fcpro_name))
) wc;

CREATE CLUSTERED INDEX IX_LaborCalcTagged
    ON #LaborCalcTagged(JobNumberRaw, OperationNumber, LaborType);

/* Job-level review totals intentionally exclude designated Inspection / Shipping work centers. */
SELECT
    JobNumberRaw,
    SUM(CASE WHEN IgnoreVariance = 0 THEN ActualHours ELSE 0 END) AS ActualHours,
    SUM(CASE WHEN IgnoreVariance = 0 THEN EstimatedHours ELSE 0 END) AS EstimatedHours
INTO #JobTotals
FROM #LaborCalcTagged
GROUP BY JobNumberRaw;

CREATE CLUSTERED INDEX IX_JobTotals
    ON #JobTotals(JobNumberRaw);
"""

    jobs_sql = """
SELECT
    sj.JobNumber,
    sj.PartNumber,
    sj.PartRevision,
    sj.JobStatus,
    sj.CompletionDate,
    sj.OperationCount,
    CAST(ISNULL(jt.ActualHours, 0) AS DECIMAL(12,2)) AS ActualHours,
    CAST(ISNULL(jt.EstimatedHours, 0) AS DECIMAL(12,2)) AS EstimatedHours,
    CAST(
        ISNULL(jt.EstimatedHours, 0) - ISNULL(jt.ActualHours, 0)
        AS DECIMAL(12,2)
    ) AS VarianceHours,
    CASE
        WHEN ABS(ISNULL(jt.ActualHours, 0)) < 0.01
          OR ABS(ISNULL(jt.EstimatedHours, 0)) < 0.01
        THEN NULL
        ELSE CAST(
            jt.EstimatedHours / NULLIF(jt.ActualHours, 0) * 100.0
            AS DECIMAL(10,1)
        )
    END AS EfficiencyPercent,
    CASE
        WHEN ABS(ISNULL(jt.ActualHours, 0)) < 0.01
          OR ABS(ISNULL(jt.EstimatedHours, 0)) < 0.01
        THEN 'DATA REVIEW'
        WHEN jt.EstimatedHours / NULLIF(jt.ActualHours, 0) * 100.0 < 80
        THEN 'BELOW 80%'
        WHEN jt.EstimatedHours / NULLIF(jt.ActualHours, 0) * 100.0 >= 125
        THEN 'HIGH PERF REVIEW'
        ELSE 'NORMAL'
    END AS ReviewStatus
FROM #SelectedJobs sj
LEFT JOIN #JobTotals jt
    ON jt.JobNumberRaw = sj.JobNumberRaw
ORDER BY
    sj.CompletionDate DESC,
    CASE
        WHEN ABS(ISNULL(jt.ActualHours, 0)) < 0.01
          OR ABS(ISNULL(jt.EstimatedHours, 0)) < 0.01 THEN 4
        WHEN jt.EstimatedHours / NULLIF(jt.ActualHours, 0) * 100.0 < 80 THEN 1
        WHEN jt.EstimatedHours / NULLIF(jt.ActualHours, 0) * 100.0 >= 125 THEN 3
        ELSE 2
    END,
    EfficiencyPercent ASC,
    sj.JobNumber;
"""

    operations_sql = """
SELECT
    LTRIM(RTRIM(lt.JobNumberRaw)) AS JobNumber,
    lt.OperationNumber,
    MAX(lt.WorkCenter) AS WorkCenter,
    MAX(lt.WorkCenterName) AS WorkCenterName,
    MAX(lt.OperationDescription) AS OperationDescription,
    MAX(lt.OperationCompletionDate) AS OperationCompletionDate,
    MAX(lt.IgnoreVariance) AS IgnoreVariance,
    CAST(SUM(CASE WHEN lt.LaborType = 'P' THEN lt.CompletedQty ELSE 0 END) AS DECIMAL(12,2)) AS ProductionQty,
    CAST(SUM(CASE WHEN lt.LaborType = 'S' THEN lt.ActualHours ELSE 0 END) AS DECIMAL(12,2)) AS SetupActualHours,
    CAST(SUM(CASE WHEN lt.LaborType = 'S' THEN lt.EstimatedHours ELSE 0 END) AS DECIMAL(12,2)) AS SetupEstimatedHours,
    CAST(SUM(CASE WHEN lt.LaborType = 'P' THEN lt.ActualHours ELSE 0 END) AS DECIMAL(12,2)) AS ProductionActualHours,
    CAST(SUM(CASE WHEN lt.LaborType = 'P' THEN lt.EstimatedHours ELSE 0 END) AS DECIMAL(12,2)) AS ProductionEstimatedHours,
    CAST(SUM(lt.ActualHours) AS DECIMAL(12,2)) AS ActualHours,
    CAST(SUM(lt.EstimatedHours) AS DECIMAL(12,2)) AS EstimatedHours,
    CASE
        WHEN MAX(lt.IgnoreVariance) = 1 THEN NULL
        ELSE CAST(SUM(lt.ActualHours) - SUM(lt.EstimatedHours) AS DECIMAL(12,2))
    END AS VarianceHours,
    CASE
        WHEN MAX(lt.IgnoreVariance) = 1 THEN NULL
        WHEN ABS(SUM(lt.ActualHours)) < 0.01
          OR ABS(SUM(lt.EstimatedHours)) < 0.01
        THEN NULL
        ELSE CAST(
            SUM(lt.EstimatedHours) / NULLIF(SUM(lt.ActualHours), 0) * 100.0
            AS DECIMAL(10,1)
        )
    END AS EfficiencyPercent,
    CASE
        WHEN MAX(lt.IgnoreVariance) = 1 THEN 'IGNORED'
        WHEN ABS(SUM(lt.ActualHours)) < 0.01
          OR ABS(SUM(lt.EstimatedHours)) < 0.01
        THEN 'DATA REVIEW'
        WHEN SUM(lt.EstimatedHours) / NULLIF(SUM(lt.ActualHours), 0) * 100.0 < 80
        THEN 'BELOW 80%'
        WHEN SUM(lt.EstimatedHours) / NULLIF(SUM(lt.ActualHours), 0) * 100.0 >= 125
        THEN 'HIGH PERF REVIEW'
        ELSE 'NORMAL'
    END AS ReviewStatus
FROM #LaborCalcTagged lt
GROUP BY
    lt.JobNumberRaw,
    lt.OperationNumber
ORDER BY
    lt.JobNumberRaw,
    lt.OperationNumber;
"""

    try:
        # IMPORTANT: execute the temp-table build and both SELECTs in ONE SQL batch.
        # pyodbc parameterized execution can run the parameterized statement in its
        # own SQL Server scope; #temp tables created there are not guaranteed to be
        # visible to a later cursor.execute() call. Keeping everything in one batch
        # preserves #SelectedJobs / #LaborAgg / #LaborCalc / #JobTotals through both
        # report SELECTs.
        report_sql = build_sql + "\n" + jobs_sql + "\n" + operations_sql
        cursor.execute(report_sql, start_value, end_value)

        result_sets = []
        while True:
            if cursor.description is not None:
                result_sets.append(rows_to_dicts(cursor, cursor.fetchall()))
                if len(result_sets) >= 2:
                    break
            if not cursor.nextset():
                break

        if len(result_sets) < 2:
            raise RuntimeError(
                "Completed Job Review SQL did not return both expected result sets."
            )

        jobs = result_sets[0]
        operations = result_sets[1]
    finally:
        conn.close()

    total_actual = sum(_safe_float(row.get("ActualHours")) for row in jobs)
    total_estimated = sum(_safe_float(row.get("EstimatedHours")) for row in jobs)

    summary = {
        "TotalJobs": len(jobs),
        "DataReviewJobs": sum(1 for row in jobs if row.get("ReviewStatus") == "DATA REVIEW"),
        "Below80Jobs": sum(1 for row in jobs if row.get("ReviewStatus") == "BELOW 80%"),
        "NormalJobs": sum(1 for row in jobs if row.get("ReviewStatus") == "NORMAL"),
        "HighPerfReviewJobs": sum(1 for row in jobs if row.get("ReviewStatus") == "HIGH PERF REVIEW"),
        "TotalActualHours": round(total_actual, 2),
        "TotalEstimatedHours": round(total_estimated, 2),
        "OverallEfficiencyPercent": round(total_estimated / total_actual * 100.0, 1) if total_actual >= 0.01 else None,
    }

    return {
        "start_date": start_value.isoformat(),
        "end_date": end_value.isoformat(),
        "summary": summary,
        "jobs": jobs,
        "operations": operations,
    }


HISTORY_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>M2M Part Labor History</title>
<style>
:root {
    --navy: #14273d;
    --navy-2: #1f3a56;
    --blue: #2767a8;
    --blue-soft: #eaf2fb;
    --ink: #1c2733;
    --muted: #667587;
    --line: #d9e1e8;
    --panel: #ffffff;
    --canvas: #eef2f6;
    --soft: #f7f9fb;
    --warning: #fff4cf;
    --warning-line: #e5b94d;
    --shadow: 0 8px 24px rgba(22, 38, 55, .08);
}
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: "Segoe UI", Arial, sans-serif;
    background: var(--canvas);
    color: var(--ink);
}
.topbar {
    background: linear-gradient(135deg, var(--navy), var(--navy-2));
    color: white;
    min-height: 72px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 0 28px;
    box-shadow: 0 3px 12px rgba(0,0,0,.14);
}
.brand { display: flex; align-items: center; gap: 12px; font-size: 20px; font-weight: 700; letter-spacing: .2px; }
.brand-mark {
    width: 42px; height: 42px; border-radius: 10px;
    display: inline-flex; align-items: center; justify-content: center;
    background: white; color: var(--navy); font-size: 13px; font-weight: 800;
}
.nav {
    display: flex; gap: 6px; flex-wrap: wrap;
    padding: 5px;
    background: rgba(255,255,255,.10);
    border: 1px solid rgba(255,255,255,.20);
    border-radius: 12px;
}
.nav a {
    text-decoration: none;
    color: #e5edf6;
    padding: 11px 20px;
    border-radius: 8px;
    font-weight: 800;
    font-size: 15px;
    min-width: 190px;
    text-align: center;
    border: 1px solid transparent;
    transition: background .15s ease, color .15s ease, transform .15s ease;
}
.nav a:hover {
    background: rgba(255,255,255,.14);
    color: white;
}
.nav a.active {
    background: white;
    color: var(--navy);
    border-color: white;
    box-shadow: 0 3px 10px rgba(0,0,0,.16);
}
.nav a.active::after {
    content: "CURRENT VIEW";
    display: block;
    margin-top: 3px;
    font-size: 9px;
    letter-spacing: 1px;
    color: var(--blue);
}
.nav a:not(.active)::after {
    content: "SWITCH VIEW";
    display: block;
    margin-top: 3px;
    font-size: 9px;
    letter-spacing: 1px;
    color: #b9cada;
}
.shell { max-width: 1540px; margin: 0 auto; padding: 28px; }
.page-hero { margin-bottom: 20px; }
.eyebrow { text-transform: uppercase; letter-spacing: 1.4px; color: var(--blue); font-size: 12px; font-weight: 800; margin-bottom: 7px; }
h1 { margin: 0; font-size: 32px; letter-spacing: -.4px; }
.subtitle { color: var(--muted); margin-top: 7px; line-height: 1.5; max-width: 900px; }
.card, .search-box, .filters, .summary, .part-header {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    box-shadow: var(--shadow); margin-bottom: 18px;
}
.search-box { padding: 18px; display: flex; align-items: end; gap: 10px; flex-wrap: wrap; }
.field-group { display: flex; flex-direction: column; gap: 6px; }
.field-label, .filter-label { font-size: 12px; font-weight: 800; text-transform: uppercase; letter-spacing: .55px; color: var(--muted); }
input, select {
    font: inherit; min-height: 42px; padding: 9px 11px; border: 1px solid #c8d2dc;
    border-radius: 8px; background: white; color: var(--ink); outline: none;
}
input:focus, select:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(39,103,168,.13); }
button {
    font: inherit; font-weight: 700; min-height: 42px; padding: 9px 14px; cursor: pointer;
    border: 1px solid #c8d2dc; border-radius: 8px; background: white; color: var(--ink);
}
button:hover { background: #f3f6f9; }
.search-box button { background: var(--blue); color: white; border-color: var(--blue); padding-inline: 20px; }
.search-box button:hover { filter: brightness(.95); }
.filters { padding: 18px; }
.filters strong { color: var(--navy); }
.filters input, .filters select, .filters button { margin: 7px 8px 7px 0; }
.quick-date-button { min-height: 36px; padding: 6px 10px; font-size: 13px; }
.summary { padding: 18px; }
.summary > strong { font-size: 18px; color: var(--navy); }
.summary-section-title { font-size: 15px; font-weight: 800; color: var(--navy); margin-bottom: 10px; }
.summary-block { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--line); }
.summary-grid { display: grid; grid-template-columns: repeat(5, minmax(140px, 1fr)); gap: 12px; }
.summary-card {
    background: var(--soft); border: 1px solid var(--line); border-radius: 10px;
    padding: 14px; text-align: left; color: var(--muted); font-size: 12px; font-weight: 700;
    text-transform: uppercase; letter-spacing: .45px;
}
.summary-value { font-size: 28px; font-weight: 800; color: var(--ink); margin-top: 6px; text-transform: none; letter-spacing: 0; }
.summary-card.efficiency-good, .operation-summary-card.efficiency-good {
    background: #edf8f2; border-color: #abd9c3;
}
.summary-card.efficiency-bad, .operation-summary-card.efficiency-bad {
    background: #fff1f1; border-color: #e5b3b3;
}
.summary-card.efficiency-good .summary-value,
.operation-summary-card.efficiency-good .operation-summary-value { color: #17633f; }
.summary-card.efficiency-bad .summary-value,
.operation-summary-card.efficiency-bad .operation-summary-value { color: #9b2f2f; }
.part-header {
    padding: 14px 18px; display: flex; gap: 24px; flex-wrap: wrap; align-items: center;
    border-left: 4px solid var(--blue);
}
.part-header strong { color: var(--navy); }
.operation {
    background: var(--panel); margin-bottom: 16px; border: 1px solid var(--line);
    border-radius: 12px; overflow: hidden; box-shadow: var(--shadow);
}
.operation-header {
    background: linear-gradient(135deg, var(--navy), var(--navy-2)); color: white;
    padding: 16px 18px; cursor: pointer; user-select: none; display: flex;
    justify-content: space-between; align-items: center; gap: 16px;
}
.operation-header:hover { filter: brightness(1.04); }
.operation-number { font-size: 22px; font-weight: 800; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.operation-workcenter { font-size: 16px; font-weight: 700; color: #c8d8e8; }
.operation-description { margin-top: 5px; line-height: 1.4; color: #d9e5ef; font-size: 13px; max-width: 1100px; }
.operation-toggle {
    font-size: 24px; font-weight: 400; min-width: 40px; min-height: 40px;
    border: 1px solid rgba(255,255,255,.22); display: flex; align-items: center;
    justify-content: center; border-radius: 8px; cursor: pointer; background: rgba(255,255,255,.06);
}
.operation-toggle:hover { background: rgba(255,255,255,.14); }
.operation-content { display: block; }
.operation-content.collapsed { display: none; }
.operation-summary {
    display: grid; grid-template-columns: repeat(6, minmax(130px, 1fr)); gap: 10px;
    padding: 14px; background: #f5f8fb; border-bottom: 1px solid var(--line);
    overflow-x: auto;
}
.operation-summary-card {
    background: white; border: 1px solid var(--line); border-radius: 9px; padding: 11px;
    color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .35px;
}
.operation-summary-value { font-size: 22px; font-weight: 800; color: var(--ink); margin-top: 5px; text-transform: none; letter-spacing: 0; }
.labor-section-title {
    padding: 10px 15px; background: var(--blue-soft); color: var(--navy); font-size: 15px;
    font-weight: 800; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
    display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
}
.section-tools { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.section-row-count { color: var(--muted); font-size: 12px; font-weight: 700; }
.clear-column-filters {
    min-height: 30px; padding: 4px 9px; font-size: 11px; border-radius: 7px;
    background: white; color: var(--navy); border: 1px solid #c8d2dc;
}
/* Keep Part Labor History column headings visible while the page scrolls. */
.table-wrap { width: 100%; overflow: visible; }
.jobs-table-wrap { overflow: visible; }
/* Keep the larger labor-history output text, but use regular weight and fit all columns on screen. */
.labor-table { width: 100%; table-layout: fixed; }
/* Keep Part Labor History column headings on one line. */
.labor-table th { white-space: nowrap; overflow-wrap: normal; word-break: normal; }
.labor-table td { white-space: normal; overflow-wrap: anywhere; }
.labor-table td { font-size: 15px; font-weight: 400; padding: 8px 5px; height: 42px; vertical-align: middle; }
/* Keep employee names fully visible on one line and maintain consistent labor-history row height. */
.labor-table tbody tr:not(.raw-average-row):not(.adjusted-average-row) { height: 42px; }
.labor-table tbody tr:not(.raw-average-row):not(.adjusted-average-row) td:nth-child(4) {
    white-space: nowrap;
    overflow: visible;
    text-overflow: clip;
    overflow-wrap: normal;
}
/* Give Employee enough width for full names while keeping the complete labor table on the page. */
.labor-table col:nth-child(1) { width: 5%; }
.labor-table col:nth-child(2) { width: 9%; }
.labor-table col:nth-child(3) { width: 8%; }
.labor-table col:nth-child(4) { width: 20%; }
.labor-table col:nth-child(5) { width: 5%; }
.labor-table col:nth-child(6) { width: 5%; }
.labor-table col:nth-child(7) { width: 5%; }
.labor-table col:nth-child(8) { width: 7%; }
.labor-table col:nth-child(9) { width: 8%; }
.labor-table col:nth-child(10) { width: 7%; }
.labor-table col:nth-child(11) { width: 8%; }
.labor-table col:nth-child(12) { width: 13%; }
.labor-table .status-badge { font-weight: 500; }
.labor-table input.column-filter { min-width: 0; width: 100%; padding-left: 5px; padding-right: 5px; }
.summary-value, .operation-summary-value { font-weight: 400; }
.labor-table thead th { position: sticky; z-index: 30; box-shadow: 0 2px 5px rgba(20,39,61,.12); }
.labor-table .column-head-row th { top: 0; }
.labor-table .filter-row th { top: 34px; z-index: 29; }
.jobs-table thead th { position: sticky; top: 0; z-index: 30; box-shadow: 0 2px 5px rgba(20,39,61,.12); }
table { width: 100%; border-collapse: separate; border-spacing: 0; min-width: 1180px; }
th {
    background: #f7f9fb; color: #526171; text-align: left; padding: 8px 8px;
    border-bottom: 1px solid var(--line); white-space: nowrap; font-size: 12px;
    text-transform: uppercase; letter-spacing: .45px; position: sticky; top: 0; z-index: 2;
}
.column-head-row th { padding: 0; }
.sort-button {
    width: 100%; min-height: 34px; padding: 8px; border: 0; border-radius: 0;
    background: transparent; color: inherit; text-align: left; font-size: 12px;
    text-transform: uppercase; letter-spacing: .45px; display: flex; align-items: center;
    justify-content: space-between; gap: 7px;
}
.sort-button:hover { background: #edf2f6; }
.sort-indicator { color: var(--blue); font-size: 12px; min-width: 12px; text-align: right; }
.filter-row th { top: 34px; z-index: 2; padding: 5px 5px 7px; background: #eef3f7; }
.column-filter {
    width: 100%; min-width: 72px; min-height: 30px; padding: 5px 7px; border-radius: 6px;
    font-size: 12px; font-weight: 500; text-transform: none; letter-spacing: normal;
    box-sizing: border-box;
}
.column-filter::placeholder { color: #91a0ae; }
td { padding: 10px 9px; border-bottom: 1px solid #e8edf2; white-space: nowrap; font-size: 15px; font-weight: 400; }
.no-section-results td { text-align: center; color: var(--muted); font-style: italic; padding: 14px; }
tbody tr:nth-child(even):not(.average-row):not(.raw-average-row):not(.adjusted-average-row):not(.analysis-below-row):not(.analysis-high-row):not(.analysis-outlier-row):not(.analysis-data-row) { background: #fbfcfd; }
tbody tr:hover:not(.average-row) { background: #f0f5fa; }
.average-row { background: #e9f0f7; font-weight: 800; }
.average-row td { padding: 10px 9px; border-top: 2px solid #b8c9d9; }
.raw-average-row { background: #f3f6f9; font-weight: 800; }
.raw-average-row td { padding: 10px 9px; border-top: 2px solid #cfd8e1; }
.adjusted-average-row { background: #e9f0f7; font-weight: 800; }
.adjusted-average-row td { padding: 10px 9px; border-top: 1px solid #b8c9d9; }
.analysis-below-row { background: #fff1f1 !important; }
.analysis-high-row { background: #eef7ff !important; }
.analysis-outlier-row { background: #fff4cf !important; }
.analysis-data-row { background: #f3f0f7 !important; }
.status-badge {
    display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 11px;
    font-weight: 800; letter-spacing: .2px; white-space: nowrap;
}
.status-normal { background: #eef2f6; color: #536273; }
.status-below { background: #f4c7c7; color: #842626; }
.status-high { background: #cfe5f8; color: #1e537f; }
.status-outlier { background: #f3c85b; color: #5f4700; }
.status-data { background: #ddd4ea; color: #563c72; }
.analysis-note { font-size: 11px; font-weight: 600; color: #6d7782; margin-left: 8px; }
.error, .loading {
    background: white; border: 1px solid var(--line); box-shadow: var(--shadow);
    padding: 16px 18px; border-radius: 10px; margin-bottom: 16px;
}
.error { color: #a32f2f; border-left: 4px solid #c74646; font-weight: 700; }
.loading { color: var(--navy); border-left: 4px solid var(--blue); font-weight: 700; }
.footer { color: #7a8794; text-align: center; font-size: 12px; padding: 18px 0 28px; }
@media (max-width: 1000px) {
    .table-wrap { overflow-x: auto; overflow-y: visible; }
    .topbar { align-items: flex-start; flex-direction: column; padding: 14px 18px; }
    .shell { padding: 18px; }
    .summary-grid { grid-template-columns: repeat(2, 1fr); }
    .operation-summary { grid-template-columns: repeat(6, minmax(130px, 1fr)); }
}
@media (max-width: 620px) {
    h1 { font-size: 27px; }
    .nav { width: 100%; }
    .nav a { flex: 1 1 140px; min-width: 0; text-align: center; font-size: 13px; padding: 10px 8px; }
    .search-box { align-items: stretch; }
    .field-group, .search-box input, .search-box button { width: 100%; }
    .summary-grid, .operation-summary { grid-template-columns: 1fr; }
    .operation-number { font-size: 18px; }
    .operation-description { font-size: 12px; }
}
</style>
</head>
<body>
<header class="topbar">
    <div class="brand"><span class="brand-mark">M2M</span><span>Labor Analytics</span></div>
    <nav class="nav">
        <a class="active" href="/">Part Labor History</a>
        <a href="/live">Live Employee Performance</a>
        <a href="/dashboard">Live Job Dashboard</a>
        <a href="/completed">Completed Job Review</a>
    </nav>
</header>
<main class="shell">
    <section class="page-hero">
        <div class="eyebrow">Manufacturing Intelligence</div>
        <h1>Part Labor History</h1>
        <div class="subtitle">Review historical labor performance by part number, revision, operation, labor type, and date.</div>
    </section>
    <div class="search-box">
        <div class="field-group">
            <label class="field-label" for="partNumber">Part Number</label>
            <input id="partNumber" type="text" placeholder="Example: 209919">
        </div>
        <button onclick="searchPart()">Search Part</button>
    </div>
    <div id="filterArea"></div>
    <div id="summaryArea"></div>
    <div id="results"></div>
    <div class="footer">Made2Manage labor analytics • Internal use</div>
</main>
<script>
let allData = [];

async function searchPart() {
    const partNumber = document.getElementById("partNumber").value.trim();
    const resultsDiv = document.getElementById("results");
    const filterArea = document.getElementById("filterArea");
    const summaryArea = document.getElementById("summaryArea");
    if (!partNumber) {
        resultsDiv.innerHTML = '<div class="error">Enter a part number.</div>';
        return;
    }
    resultsDiv.innerHTML = '<div class="loading">Loading labor history...</div>';
    filterArea.innerHTML = "";
    summaryArea.innerHTML = "";
    try {
        const response = await fetch("/api/part-labor-history/" + encodeURIComponent(partNumber));
        if (!response.ok) throw new Error("No labor history found.");
        const rawData = await response.json();
        allData = combineLaborEntries(rawData);
        buildFilters(allData);
        applyFilters();
    } catch (error) {
        resultsDiv.innerHTML = '<div class="error">' + error.message + '</div>';
    }
}

function combineLaborEntries(rows) {
    const groups = {};

    rows.forEach(row => {
        const laborDate = row.LaborDate ? row.LaborDate.substring(0, 10) : "";
        const isSetup = row.LaborType === "Setup";

        // Production keeps the existing grouping.
        // Setup becomes ONE row per Job + Operation + Work Center.
        // This sums all setup Actual Hours while using one setup estimate.
        const key = isSetup
            ? [row.JobNumber, row.OperationNumber, row.WorkCenter || "", row.LaborType, row.PartRevision].join("|")
            : [row.JobNumber, row.OperationNumber, row.EmployeeNumber, laborDate, row.LaborType, row.PartRevision].join("|");

        if (!groups[key]) {
            groups[key] = {
                PartNumber: row.PartNumber,
                PartRevision: row.PartRevision,
                JobNumber: row.JobNumber,
                OperationNumber: row.OperationNumber,
                WorkCenter: row.WorkCenter,
                WorkCenterName: row.WorkCenterName,
                OperationDescription: row.OperationDescription,
                EmployeeNumber: row.EmployeeNumber,
                EmployeeName: row.EmployeeName,
                LaborType: row.LaborType,
                LaborDate: row.LaborDate,
                StartDateTime: row.StartDateTime,
                EndDateTime: row.EndDateTime,
                QuantityComplete: 0,
                ScrapQuantity: 0,
                ActualHours: 0,
                EstimatedHours: 0,
                VarianceHours: 0,
                EfficiencyPercent: 0,
                EntryCount: 0,
                _EmployeeNames: [],
                _EmployeeNumbers: [],
                _SetupEstimateCaptured: false
            };
        }

        const group = groups[key];

        group.QuantityComplete += Number(row.QuantityComplete) || 0;
        group.ScrapQuantity += Number(row.ScrapQuantity) || 0;
        group.ActualHours += Number(row.ActualHours) || 0;

        const estimated = Number(row.EstimatedHours) || 0;

        if (isSetup) {
            // The setup estimate may be repeated on every setup entry.
            // Capture it ONCE so average estimated setup hours are not inflated.
            if (!group._SetupEstimateCaptured && estimated > 0) {
                group.EstimatedHours = estimated;
                group._SetupEstimateCaptured = true;
            }
        } else {
            group.EstimatedHours += estimated;
        }

        group.EntryCount += 1;

        const employeeName = String(row.EmployeeName || "").trim();
        const employeeNumber = String(row.EmployeeNumber || "").trim();

        if (employeeName && !group._EmployeeNames.includes(employeeName)) {
            group._EmployeeNames.push(employeeName);
        }
        if (employeeNumber && !group._EmployeeNumbers.includes(employeeNumber)) {
            group._EmployeeNumbers.push(employeeNumber);
        }

        if (row.LaborDate && (!group.LaborDate || row.LaborDate < group.LaborDate)) {
            group.LaborDate = row.LaborDate;
        }
        if (row.StartDateTime && (!group.StartDateTime || row.StartDateTime < group.StartDateTime)) {
            group.StartDateTime = row.StartDateTime;
        }
        if (row.EndDateTime && (!group.EndDateTime || row.EndDateTime > group.EndDateTime)) {
            group.EndDateTime = row.EndDateTime;
        }
    });

    const combinedRows = Object.values(groups);

    combinedRows.forEach(row => {
        if (row.LaborType === "Setup") {
            row.EmployeeName = row._EmployeeNames.join(", ");
            row.EmployeeNumber = row._EmployeeNumbers.join(", ");
        }

        delete row._EmployeeNames;
        delete row._EmployeeNumbers;
        delete row._SetupEstimateCaptured;

        row.VarianceHours = row.ActualHours - row.EstimatedHours;
        row.EfficiencyPercent = row.ActualHours > 0
            ? (row.EstimatedHours / row.ActualHours) * 100
            : 0;
    });

    combinedRows.sort((a, b) => {
        const opDifference = Number(a.OperationNumber) - Number(b.OperationNumber);
        if (opDifference !== 0) return opDifference;
        return (a.LaborDate || "").localeCompare(b.LaborDate || "");
    });

    return combinedRows;
}

function buildFilters(data) {
    const revisions = [...new Set(data.map(row => row.PartRevision))].sort();
    const operations = [...new Set(data.map(row => row.OperationNumber))].sort((a, b) => Number(a) - Number(b));
    let revisionOptions = '<option value="">All Revisions</option>';
    revisions.forEach(rev => revisionOptions += `<option value="${rev}">${rev}</option>`);
    let operationOptions = '<option value="">All Operations</option>';
    operations.forEach(op => operationOptions += `<option value="${op}">Operation ${op}</option>`);
    document.getElementById("filterArea").innerHTML = `
        <div class="filters">
            <strong>Filters:</strong><br><br>
            <span class="filter-label">Revision:</span>
            <select id="revisionFilter" onchange="applyFilters()">${revisionOptions}</select>
            <span class="filter-label">Operation:</span>
            <select id="operationFilter" onchange="applyFilters()">${operationOptions}</select>
            <span class="filter-label">Labor Type:</span>
            <select id="laborTypeFilter" onchange="applyFilters()">
                <option value="">All Labor</option>
                <option value="Setup">Setup</option>
                <option value="Production">Production</option>
            </select><br>
            <span class="filter-label">From Date:</span>
            <input id="fromDateFilter" type="date" onchange="applyFilters()">
            <span class="filter-label">To Date:</span>
            <input id="toDateFilter" type="date" onchange="applyFilters()"><br>
            <strong>Quick Dates:</strong>
            <button class="quick-date-button" onclick="setDatePreset(30)">Last 30 Days</button>
            <button class="quick-date-button" onclick="setDatePreset(90)">Last 90 Days</button>
            <button class="quick-date-button" onclick="setDatePreset(365)">Last 1 Year</button>
            <button class="quick-date-button" onclick="setAllTime()">All Time</button><br>
            <button onclick="clearFilters()">Clear Filters</button>
            <button onclick="expandAll()">Expand All</button>
            <button onclick="collapseAll()">Collapse All</button>
        </div>`;
}

function formatDateForInput(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return year + "-" + month + "-" + day;
}

function setDatePreset(days) {
    const today = new Date();
    const fromDate = new Date();
    fromDate.setDate(today.getDate() - days);
    document.getElementById("fromDateFilter").value = formatDateForInput(fromDate);
    document.getElementById("toDateFilter").value = formatDateForInput(today);
    applyFilters();
}

function setAllTime() {
    document.getElementById("fromDateFilter").value = "";
    document.getElementById("toDateFilter").value = "";
    applyFilters();
}

function clearFilters() {
    document.getElementById("revisionFilter").value = "";
    document.getElementById("operationFilter").value = "";
    document.getElementById("laborTypeFilter").value = "";
    document.getElementById("fromDateFilter").value = "";
    document.getElementById("toDateFilter").value = "";
    applyFilters();
}

function applyFilters() {
    const revision = document.getElementById("revisionFilter") ? document.getElementById("revisionFilter").value : "";
    const operation = document.getElementById("operationFilter") ? document.getElementById("operationFilter").value : "";
    const laborType = document.getElementById("laborTypeFilter") ? document.getElementById("laborTypeFilter").value : "";
    const fromDate = document.getElementById("fromDateFilter") ? document.getElementById("fromDateFilter").value : "";
    const toDate = document.getElementById("toDateFilter") ? document.getElementById("toDateFilter").value : "";
    const filteredData = allData.filter(row => {
        const laborDate = row.LaborDate ? row.LaborDate.substring(0, 10) : "";

        // Hide zero-quantity production/other labor records.
        // Setup records are preserved because setup labor normally has Qty = 0.
        const quantityMatch = row.LaborType === "Setup" || Number(row.QuantityComplete || 0) > 0;

        return quantityMatch
            && (!revision || row.PartRevision === revision)
            && (!operation || String(row.OperationNumber) === operation)
            && (!laborType || row.LaborType === laborType)
            && (!fromDate || (laborDate && laborDate >= fromDate))
            && (!toDate || (laborDate && laborDate <= toDate));
    });
    displaySummary(filteredData);
    displayResults(filteredData);
}

function averageOf(rows, field) {
    const values = rows.map(row => Number(row[field])).filter(value => !isNaN(value));
    if (!values.length) return 0;
    return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function totalOf(rows, field) {
    return rows.map(row => Number(row[field]) || 0).reduce((sum, value) => sum + value, 0);
}

function weightedEfficiency(rows) {
    const totalActual = totalOf(rows, "ActualHours");
    const totalEstimated = totalOf(rows, "EstimatedHours");
    if (totalActual <= 0) return 0;
    return (totalEstimated / totalActual) * 100;
}

function efficiencyStatusClass(efficiency) {
    return Number(efficiency) >= 85 ? "efficiency-good" : "efficiency-bad";
}

function uniqueJobs(rows) {
    return new Set(rows.map(row => row.JobNumber)).size;
}

function median(values) {
    const nums = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
    if (!nums.length) return 0;
    const middle = Math.floor(nums.length / 2);
    return nums.length % 2 ? nums[middle] : (nums[middle - 1] + nums[middle]) / 2;
}

function robustRatioStatsFromDomRows(rows) {
    const ratios = rows.map(row => {
        const actual = Number(row.dataset.actual);
        const estimated = Number(row.dataset.estimated);
        return (Number.isFinite(actual) && Number.isFinite(estimated) && actual > 0 && estimated > 0)
            ? actual / estimated
            : NaN;
    }).filter(Number.isFinite);

    const center = median(ratios);
    const deviations = ratios.map(value => Math.abs(value - center));
    const mad = median(deviations);
    return {
        median: center,
        mad: mad,
        enabled: ratios.length >= 5 && mad > 0
    };
}

function classifyLaborDomRow(row, stats) {
    const actual = Number(row.dataset.actual);
    const estimated = Number(row.dataset.estimated);
    const efficiency = Number(row.dataset.efficiency);

    if (!Number.isFinite(actual) || !Number.isFinite(estimated) || actual <= 0 || estimated <= 0) {
        return {key: "data", label: "DATA REVIEW", exclude: true};
    }

    const ratio = actual / estimated;
    if (stats.enabled) {
        const modifiedZ = 0.6745 * (ratio - stats.median) / stats.mad;
        if (Math.abs(modifiedZ) > 3.5) {
            return {key: "outlier", label: "STAT OUTLIER", exclude: true};
        }
    }

    if (Number.isFinite(efficiency) && efficiency < 80) {
        return {key: "below", label: "BELOW 80%", exclude: false};
    }

    if (ratio <= 0.80) {
        return {key: "high", label: "HIGH PERF REVIEW", exclude: false};
    }

    return {key: "normal", label: "NORMAL", exclude: false};
}

function statusBadge(status) {
    return `<span class="status-badge status-${status.key}">${status.label}</span>`;
}

function averageDomField(rows, datasetField) {
    const values = rows.map(row => Number(row.dataset[datasetField])).filter(Number.isFinite);
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
}

function weightedEfficiencyDom(rows) {
    const totalActual = rows.reduce((sum, row) => sum + (Number(row.dataset.actual) || 0), 0);
    const totalEstimated = rows.reduce((sum, row) => sum + (Number(row.dataset.estimated) || 0), 0);
    return totalActual > 0 ? (totalEstimated / totalActual) * 100 : 0;
}

function buildSummaryCards(rows, title) {
    const jobs = uniqueJobs(rows);
    const totalActual = totalOf(rows, "ActualHours");
    const totalEstimated = totalOf(rows, "EstimatedHours");
    const variance = totalActual - totalEstimated;
    const efficiency = weightedEfficiency(rows);
    return `
        <div class="summary-block">
            <div class="summary-section-title">${title}</div>
            <div class="summary-grid">
                <div class="summary-card">Jobs<div class="summary-value">${jobs}</div></div>
                <div class="summary-card">Actual Hrs<div class="summary-value">${totalActual.toFixed(2)}</div></div>
                <div class="summary-card">Estimated Hrs<div class="summary-value">${totalEstimated.toFixed(2)}</div></div>
                <div class="summary-card">Variance<div class="summary-value">${variance.toFixed(2)}</div></div>
                <div class="summary-card ${efficiencyStatusClass(efficiency)}">Weighted Efficiency<div class="summary-value">${efficiency.toFixed(1)}%</div></div>
            </div>
        </div>`;
}

function displaySummary(data) {
    const summaryArea = document.getElementById("summaryArea");
    if (!data.length) { summaryArea.innerHTML = ""; return; }
    const setupRows = data.filter(row => row.LaborType === "Setup");
    const productionRows = data.filter(row => row.LaborType === "Production");
    let html = '<div class="summary"><strong>Filtered Labor Summary</strong>';
    if (setupRows.length) html += buildSummaryCards(setupRows, "Setup Summary");
    if (productionRows.length) html += buildSummaryCards(productionRows, "Production Summary");
    html += '</div>';
    summaryArea.innerHTML = html;
}

function toggleOperation(operationId) {
    const content = document.getElementById("operation-content-" + operationId);
    const toggle = document.getElementById("operation-toggle-" + operationId);
    if (!content || !toggle) return;
    if (content.classList.contains("collapsed")) {
        content.classList.remove("collapsed");
        toggle.innerHTML = "−";
    } else {
        content.classList.add("collapsed");
        toggle.innerHTML = "+";
    }
}

function expandAll() {
    document.querySelectorAll(".operation-content").forEach(content => content.classList.remove("collapsed"));
    document.querySelectorAll(".operation-toggle").forEach(toggle => toggle.innerHTML = "−");
}

function collapseAll() {
    document.querySelectorAll(".operation-content").forEach(content => content.classList.add("collapsed"));
    document.querySelectorAll(".operation-toggle").forEach(toggle => toggle.innerHTML = "+");
}

function compareFilterValue(cellValue, filterValue, type) {
    const rawFilter = String(filterValue || "").trim();
    if (!rawFilter) return true;

    const rawCell = String(cellValue ?? "").trim();
    if (type === "number") {
        const match = rawFilter.match(/^(<=|>=|=|<|>)?\s*(-?\d+(?:\.\d+)?)$/);
        if (match) {
            const operator = match[1] || "=";
            const target = Number(match[2]);
            const value = Number(rawCell.replace(/%/g, ""));
            if (Number.isNaN(value)) return false;
            if (operator === ">") return value > target;
            if (operator === ">=") return value >= target;
            if (operator === "<") return value < target;
            if (operator === "<=") return value <= target;
            return value === target;
        }
    }

    if (type === "date") {
        const match = rawFilter.match(/^(<=|>=|=|<|>)?\s*(\d{4}-\d{1,2}(?:-\d{1,2})?)$/);
        if (match && rawCell) {
            const operator = match[1] || "=";
            const target = match[2];
            if (operator === ">") return rawCell > target;
            if (operator === ">=") return rawCell >= target;
            if (operator === "<") return rawCell < target;
            if (operator === "<=") return rawCell <= target;
            return rawCell.startsWith(target);
        }
    }

    return rawCell.toLowerCase().includes(rawFilter.toLowerCase());
}

function applySectionColumnFilters(tableId) {
    const table = document.getElementById(tableId);
    if (!table) return;
    const filters = Array.from(table.querySelectorAll(".column-filter"));
    const rows = Array.from(table.querySelectorAll("tbody tr.data-row"));

    rows.forEach(row => {
        const show = filters.every(input => {
            const filterValue = input.value;
            if (!filterValue.trim()) return true;
            const col = Number(input.dataset.col);
            const cell = row.cells[col];
            const cellValue = cell ? (cell.dataset.sortValue ?? cell.textContent) : "";
            return compareFilterValue(cellValue, filterValue, input.dataset.type || "text");
        });
        row.style.display = show ? "" : "none";
    });

    refreshSectionAverage(tableId);
}

function clearSectionColumnFilters(tableId) {
    const table = document.getElementById(tableId);
    if (!table) return;
    table.querySelectorAll(".column-filter").forEach(input => input.value = "");
    table.querySelectorAll("tbody tr.data-row").forEach(row => row.style.display = "");
    refreshSectionAverage(tableId);
}

function sortLaborTable(tableId, columnIndex, type, button) {
    const table = document.getElementById(tableId);
    if (!table) return;
    const tbody = table.querySelector("tbody");
    const rows = Array.from(tbody.querySelectorAll("tr.data-row"));
    const currentColumn = table.dataset.sortColumn;
    const currentDirection = table.dataset.sortDirection || "asc";
    const nextDirection = currentColumn === String(columnIndex) && currentDirection === "asc" ? "desc" : "asc";

    const getValue = row => {
        const cell = row.cells[columnIndex];
        const raw = cell ? (cell.dataset.sortValue ?? cell.textContent) : "";
        if (type === "number") {
            const value = Number(String(raw).replace(/%/g, ""));
            return Number.isNaN(value) ? -Infinity : value;
        }
        return String(raw).trim().toLowerCase();
    };

    rows.sort((a, b) => {
        const av = getValue(a);
        const bv = getValue(b);
        let result = 0;
        if (type === "number") result = av - bv;
        else result = av.localeCompare(bv, undefined, {numeric: true, sensitivity: "base"});
        return nextDirection === "asc" ? result : -result;
    });

    const averageRow = tbody.querySelector("tr.raw-average-row");
    rows.forEach(row => tbody.insertBefore(row, averageRow));
    table.dataset.sortColumn = String(columnIndex);
    table.dataset.sortDirection = nextDirection;

    table.querySelectorAll(".sort-button").forEach(btn => {
        btn.setAttribute("aria-sort", "none");
        const indicator = btn.querySelector(".sort-indicator");
        if (indicator) indicator.textContent = "";
    });
    if (button) {
        button.setAttribute("aria-sort", nextDirection === "asc" ? "ascending" : "descending");
        const indicator = button.querySelector(".sort-indicator");
        if (indicator) indicator.textContent = nextDirection === "asc" ? "▲" : "▼";
    }
}

function refreshSectionAverage(tableId) {
    const table = document.getElementById(tableId);
    if (!table) return;

    const allRows = Array.from(table.querySelectorAll("tbody tr.data-row"));
    const stats = robustRatioStatsFromDomRows(allRows);

    let belowCount = 0;
    let highCount = 0;
    let outlierCount = 0;
    let dataReviewCount = 0;

    allRows.forEach(row => {
        const status = classifyLaborDomRow(row, stats);
        row.dataset.analysisStatus = status.key;
        row.dataset.excludeAdjusted = status.exclude ? "1" : "0";
        row.classList.remove("analysis-below-row", "analysis-high-row", "analysis-outlier-row", "analysis-data-row");

        if (status.key === "below") {
            row.classList.add("analysis-below-row");
            belowCount += 1;
        } else if (status.key === "high") {
            row.classList.add("analysis-high-row");
            highCount += 1;
        } else if (status.key === "outlier") {
            row.classList.add("analysis-outlier-row");
            outlierCount += 1;
        } else if (status.key === "data") {
            row.classList.add("analysis-data-row");
            dataReviewCount += 1;
        }

        const statusCell = row.querySelector(".analysis-status");
        if (statusCell) {
            statusCell.dataset.sortValue = status.label;
            statusCell.innerHTML = statusBadge(status);
        }
    });

    const visibleRows = allRows.filter(row => row.style.display !== "none");
    const adjustedRows = visibleRows.filter(row => row.dataset.excludeAdjusted !== "1");

    const countLabel = document.getElementById(tableId + "-count");
    if (countLabel) {
        countLabel.textContent = `${visibleRows.length} of ${allRows.length} row${allRows.length === 1 ? "" : "s"}`;
    }

    const rawRow = table.querySelector("tr.raw-average-row");
    const adjustedRow = table.querySelector("tr.adjusted-average-row");
    if (!rawRow || !adjustedRow) return;

    const title = table.dataset.sectionTitle || "Labor";

    if (!visibleRows.length) {
        rawRow.querySelector(".avg-label").innerHTML = "No rows match column filters";
        adjustedRow.querySelector(".avg-label").innerHTML = "No rows match column filters";
        [rawRow, adjustedRow].forEach(summaryRow => {
            summaryRow.querySelector(".avg-actual").textContent = "—";
            summaryRow.querySelector(".avg-estimated").textContent = "—";
            summaryRow.querySelector(".avg-variance").textContent = "—";
            summaryRow.querySelector(".avg-efficiency").textContent = "—";
        });
        return;
    }

    const setSummary = (summaryRow, rows, label) => {
        const avgActual = averageDomField(rows, "actual");
        const avgEstimated = averageDomField(rows, "estimated");
        const avgVariance = averageDomField(rows, "variance");
        const efficiency = weightedEfficiencyDom(rows);

        summaryRow.querySelector(".avg-label").innerHTML = label;
        summaryRow.querySelector(".avg-actual").textContent = rows.length ? avgActual.toFixed(2) : "—";
        summaryRow.querySelector(".avg-estimated").textContent = rows.length ? avgEstimated.toFixed(2) : "—";
        summaryRow.querySelector(".avg-variance").textContent = rows.length ? avgVariance.toFixed(2) : "—";
        summaryRow.querySelector(".avg-efficiency").textContent = rows.length ? `${efficiency.toFixed(1)}%` : "—";
    };

    setSummary(rawRow, visibleRows, `Raw ${title} Average`);

    const excludedVisible = visibleRows.length - adjustedRows.length;
    const adjustedNote = `<span class="analysis-note">${adjustedRows.length} of ${visibleRows.length} used • ${excludedVisible} excluded</span>`;
    setSummary(adjustedRow, adjustedRows, `Adjusted ${title} Average ${adjustedNote}`);

    const analysisLabel = document.getElementById(tableId + "-analysis");
    if (analysisLabel) {
        analysisLabel.innerHTML =
            `<strong>${belowCount}</strong> below 80% • ` +
            `<strong>${highCount}</strong> high-performance review • ` +
            `<strong>${outlierCount}</strong> statistical outlier${outlierCount === 1 ? "" : "s"} • ` +
            `<strong>${dataReviewCount}</strong> data review`;
    }
}

function initializeLaborTables() {
    document.querySelectorAll("table.labor-table").forEach(table => refreshSectionAverage(table.id));
}

function renderLaborSection(rows, title, sectionId) {
    if (!rows.length) return "";
    const safeId = `labor-table-${String(sectionId).replace(/[^a-zA-Z0-9_-]/g, "_")}`;

    const columns = [
        ["Revision", "text", "Filter"],
        ["Job", "text", "Filter"],
        ["Date", "date", "YYYY-MM"],
        ["Employee", "text", "Filter"],
        ["Entries", "number", ">= 1"],
        ["Qty", "number", ">= 10"],
        ["Scrap", "number", "> 0"],
        ["Actual Hrs", "number", "> 1"],
        ["Estimated Hrs", "number", "> 1"],
        ["Variance", "number", "> 0"],
        ["Efficiency", "number", ">= 80"],
        ["Status", "text", "Filter"]
    ];

    let html = `
        <div class="labor-section-title">
            <span>${title}
                ${title === "Setup"
                    ? '<span class="analysis-note">Setup: all entries for each job are combined; one setup estimate is used per job/operation.</span>'
                    : ''}
                <span class="analysis-note" id="${safeId}-analysis">
                    Analysis: &lt;80% = performance review • ≥125% = high-performance review • MAD |Z| &gt; 3.5 = statistical outlier
                </span>
            </span>
            <div class="section-tools">
                <span class="section-row-count" id="${safeId}-count">${rows.length} of ${rows.length} row${rows.length === 1 ? "" : "s"}</span>
                <button type="button" class="clear-column-filters" onclick="clearSectionColumnFilters('${safeId}')">Clear column filters</button>
            </div>
        </div>
        <div class="table-wrap"><table class="labor-table" id="${safeId}" data-section-title="${title}">
        <colgroup>
            <col><col><col><col><col><col><col><col><col><col><col><col>
        </colgroup>
        <thead>
            <tr class="column-head-row">`;

    columns.forEach((column, index) => {
        html += `<th><button type="button" class="sort-button" aria-sort="none" onclick="sortLaborTable('${safeId}', ${index}, '${column[1]}', this)">${column[0]}<span class="sort-indicator"></span></button></th>`;
    });

    html += `</tr><tr class="filter-row">`;
    columns.forEach((column, index) => {
        html += `<th><input class="column-filter" type="text" data-col="${index}" data-type="${column[1]}" placeholder="${column[2]}" oninput="applySectionColumnFilters('${safeId}')" aria-label="Filter ${column[0]}"></th>`;
    });
    html += `</tr></thead><tbody>`;

    rows.forEach(row => {
        const date = row.LaborDate ? row.LaborDate.substring(0, 10) : "";
        const actualNumber = Number(row.ActualHours || 0);
        const estimatedNumber = Number(row.EstimatedHours || 0);
        const varianceNumber = Number(row.VarianceHours || 0);
        const efficiencyNumber = Number(row.EfficiencyPercent || 0);

        html += `<tr class="data-row"
            data-actual="${actualNumber}"
            data-estimated="${estimatedNumber}"
            data-variance="${varianceNumber}"
            data-efficiency="${efficiencyNumber}">
            <td data-sort-value="${row.PartRevision || ""}">${row.PartRevision || ""}</td>
            <td data-sort-value="${row.JobNumber || ""}">${row.JobNumber || ""}</td>
            <td data-sort-value="${date}">${date}</td>
            <td data-sort-value="${row.EmployeeName || ""}">${row.EmployeeName || ""}</td>
            <td data-sort-value="${Number(row.EntryCount || 0)}">${Number(row.EntryCount || 0)}</td>
            <td data-sort-value="${Number(row.QuantityComplete || 0)}">${Number(row.QuantityComplete || 0).toFixed(0)}</td>
            <td data-sort-value="${Number(row.ScrapQuantity || 0)}">${Number(row.ScrapQuantity || 0).toFixed(0)}</td>
            <td data-sort-value="${actualNumber}">${actualNumber.toFixed(2)}</td>
            <td data-sort-value="${estimatedNumber}">${estimatedNumber.toFixed(2)}</td>
            <td data-sort-value="${varianceNumber}">${varianceNumber.toFixed(2)}</td>
            <td data-sort-value="${efficiencyNumber}">${efficiencyNumber.toFixed(1)}%</td>
            <td class="analysis-status" data-sort-value=""></td>
        </tr>`;
    });

    html += `
        <tr class="raw-average-row">
            <td colspan="7" class="avg-label">Raw ${title} Average</td>
            <td class="avg-actual"></td>
            <td class="avg-estimated"></td>
            <td class="avg-variance"></td>
            <td class="avg-efficiency"></td>
            <td></td>
        </tr>
        <tr class="adjusted-average-row">
            <td colspan="7" class="avg-label">Adjusted ${title} Average</td>
            <td class="avg-actual"></td>
            <td class="avg-estimated"></td>
            <td class="avg-variance"></td>
            <td class="avg-efficiency"></td>
            <td></td>
        </tr>
    </tbody></table></div>`;

    return html;
}

function displayResults(data) {
    const resultsDiv = document.getElementById("results");
    if (!data.length) {
        resultsDiv.innerHTML = '<div class="error">No records match the selected filters.</div>';
        return;
    }
    const partNumber = data[0].PartNumber;
    let html = `<div class="part-header"><strong>Part Number:</strong> ${partNumber}&nbsp;&nbsp;&nbsp;<strong>Combined Labor Records:</strong> ${data.length}</div>`;
    const operations = {};
    data.forEach(row => {
        const operation = row.OperationNumber;
        if (!operations[operation]) operations[operation] = [];
        operations[operation].push(row);
    });
    const operationNumbers = Object.keys(operations).sort((a, b) => Number(a) - Number(b));
    operationNumbers.forEach(operationNumber => {
        const rows = operations[operationNumber];
        const description = rows[0].OperationDescription || "";
        const workCenterLabels = [...new Set(rows.map(row => {
            const id = (row.WorkCenter || "").trim();
            const name = (row.WorkCenterName || "").trim();
            if (name && id) return `WC ${id} • ${name}`;
            if (name) return name;
            if (id) return `WC ${id}`;
            return "";
        }).filter(Boolean))];
        const workCenterText = workCenterLabels.join(" / ");
        const setupRows = rows.filter(row => row.LaborType === "Setup");
        const productionRows = rows.filter(row => row.LaborType === "Production");
        const otherRows = rows.filter(row => row.LaborType !== "Setup" && row.LaborType !== "Production");
        const operationId = String(operationNumber).replace(/[^a-zA-Z0-9]/g, "");
        const totalActual = totalOf(rows, "ActualHours");
        const totalEstimated = totalOf(rows, "EstimatedHours");
        const totalVariance = totalActual - totalEstimated;
        const setupEfficiency = setupRows.length ? weightedEfficiency(setupRows) : null;
        const productionEfficiency = productionRows.length ? weightedEfficiency(productionRows) : null;
        const jobs = uniqueJobs(rows);
        html += `<div class="operation">
            <div class="operation-header" onclick="toggleOperation('${operationId}')">
                <div><div class="operation-number">Operation ${operationNumber}${workCenterText ? `<span class="operation-workcenter">• ${workCenterText}</span>` : ``}</div><div class="operation-description">${description}</div></div>
                <div class="operation-toggle" id="operation-toggle-${operationId}" onclick="event.stopPropagation();toggleOperation('${operationId}')">+</div>
            </div>
            <div class="operation-content collapsed" id="operation-content-${operationId}">
                <div class="operation-summary">
                    <div class="operation-summary-card">Jobs<div class="operation-summary-value">${jobs}</div></div>
                    <div class="operation-summary-card">Actual Hrs<div class="operation-summary-value">${totalActual.toFixed(2)}</div></div>
                    <div class="operation-summary-card">Estimated Hrs<div class="operation-summary-value">${totalEstimated.toFixed(2)}</div></div>
                    <div class="operation-summary-card">Variance<div class="operation-summary-value">${totalVariance.toFixed(2)}</div></div>
                    ${setupEfficiency !== null ? `<div class="operation-summary-card ${efficiencyStatusClass(setupEfficiency)}">Setup Efficiency<div class="operation-summary-value">${setupEfficiency.toFixed(1)}%</div></div>` : ``}
                    ${productionEfficiency !== null ? `<div class="operation-summary-card ${efficiencyStatusClass(productionEfficiency)}">Production Efficiency<div class="operation-summary-value">${productionEfficiency.toFixed(1)}%</div></div>` : ``}
                </div>`;
        html += renderLaborSection(setupRows, "Setup", `${operationId}-setup`);
        html += renderLaborSection(productionRows, "Production", `${operationId}-production`);
        if (otherRows.length) html += renderLaborSection(otherRows, "Other Labor", `${operationId}-other`);
        html += `</div></div>`;
    });
    resultsDiv.innerHTML = html;
    initializeLaborTables();
}

document.getElementById("partNumber").addEventListener("keydown", function(event) {
    if (event.key === "Enter") searchPart();
});
</script>
</body>
</html>"""


LIVE_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Employee Performance</title>
<style>
:root {
    --navy: #14273d;
    --navy-2: #1f3a56;
    --blue: #2767a8;
    --blue-soft: #eaf2fb;
    --ink: #1c2733;
    --muted: #667587;
    --line: #d9e1e8;
    --panel: #ffffff;
    --canvas: #eef2f6;
    --soft: #f7f9fb;
    --good-bg: #edf8f2;
    --good-line: #abd9c3;
    --good-text: #17633f;
    --bad-bg: #fff1f1;
    --bad-line: #e5b3b3;
    --bad-text: #9b2f2f;
    --shadow: 0 7px 20px rgba(22, 38, 55, .08);
}
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: "Segoe UI", Arial, sans-serif;
    background: var(--canvas);
    color: var(--ink);
}
.topbar {
    background: linear-gradient(135deg, var(--navy), var(--navy-2));
    color: white;
    min-height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    padding: 9px 24px;
    box-shadow: 0 3px 11px rgba(0,0,0,.14);
}
.brand { display: flex; align-items: center; gap: 11px; font-size: 19px; font-weight: 750; }
.brand-mark {
    width: 38px; height: 38px; border-radius: 9px; display: inline-flex; align-items: center;
    justify-content: center; background: white; color: var(--navy); font-size: 12px; font-weight: 850;
}
.nav {
    display: flex; gap: 6px; flex-wrap: wrap; padding: 5px;
    background: rgba(255,255,255,.10); border: 1px solid rgba(255,255,255,.20); border-radius: 11px;
}
.nav a {
    text-decoration: none; color: #e5edf6; padding: 9px 18px; border-radius: 8px;
    font-weight: 800; font-size: 14px; min-width: 182px; text-align: center; border: 1px solid transparent;
}
.nav a:hover { background: rgba(255,255,255,.14); color: white; }
.nav a.active { background: white; color: var(--navy); border-color: white; box-shadow: 0 3px 9px rgba(0,0,0,.15); }
.nav a.active::after, .nav a:not(.active)::after {
    display: block; margin-top: 2px; font-size: 8px; letter-spacing: .9px;
}
.nav a.active::after { content: "CURRENT VIEW"; color: var(--blue); }
.nav a:not(.active)::after { content: "SWITCH VIEW"; color: #b9cada; }
.shell { max-width: 1450px; margin: 0 auto; padding: 16px 22px 10px; }
.page-hero {
    display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; margin-bottom: 12px;
}
.eyebrow { text-transform: uppercase; letter-spacing: 1.2px; color: var(--blue); font-size: 11px; font-weight: 850; }
h1 { margin: 0; font-size: 27px; letter-spacing: -.35px; }
.subtitle { color: var(--muted); font-size: 13px; margin: 0; line-height: 1.4; flex: 1 1 520px; }
.search-box {
    background: white; padding: 12px 14px; border: 1px solid var(--line); border-radius: 11px;
    margin-bottom: 12px; box-shadow: var(--shadow); display: flex; align-items: end; gap: 10px; flex-wrap: wrap;
}
.field-group { display: flex; flex-direction: column; gap: 4px; }
.field-label, .label {
    font-size: 11px; font-weight: 850; text-transform: uppercase; letter-spacing: .5px; color: var(--muted);
}
input {
    font: inherit; min-height: 42px; padding: 9px 11px; border: 1px solid #c8d2dc;
    border-radius: 8px; background: white; color: var(--ink); outline: none;
}
input:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(39,103,168,.12); }
button {
    font: inherit; font-weight: 750; min-height: 42px; padding: 9px 15px; cursor: pointer;
    border: 1px solid var(--blue); border-radius: 8px; background: var(--blue); color: white;
}
button:hover { filter: brightness(.96); }
.error, .loading, .notice {
    background: white; border: 1px solid var(--line); padding: 12px 14px; border-radius: 9px;
    margin-bottom: 10px; box-shadow: var(--shadow); font-size: 13px;
}
.error { color: #a32f2f; border-left: 4px solid #c74646; font-weight: 700; }
.loading { color: var(--navy); border-left: 4px solid var(--blue); font-weight: 700; }
.notice { border-left: 4px solid #8a97a5; color: #4f5c69; }
.job-selector {
    display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-bottom: 10px;
    background: white; border: 1px solid var(--line); border-radius: 10px; padding: 9px 11px; box-shadow: var(--shadow);
}
.job-selector-label { font-size: 12px; font-weight: 850; color: var(--muted); margin-right: 4px; }
.job-selector button {
    min-height: 35px; padding: 6px 11px; background: white; color: var(--navy); border-color: #bdc9d5; font-size: 12px;
}
.job-selector button.selected { background: var(--navy); color: white; border-color: var(--navy); }
.job-card {
    background: white; border: 1px solid var(--line); border-radius: 12px; margin-bottom: 9px;
    box-shadow: var(--shadow); overflow: hidden;
}
.job-header {
    background: linear-gradient(135deg, var(--navy), var(--navy-2)); color: white;
    padding: 13px 16px; display: flex; justify-content: space-between; align-items: center; gap: 14px;
}
.job-title { font-size: 21px; font-weight: 850; letter-spacing: -.2px; }
.job-subtitle { margin-top: 4px; line-height: 1.3; color: #d9e5ef; font-size: 13px; }
.live-pill {
    display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border-radius: 999px;
    border: 1px solid rgba(255,255,255,.30); background: rgba(255,255,255,.10);
    font-size: 10px; font-weight: 850; letter-spacing: .65px; white-space: nowrap;
}
.live-dot { width: 8px; height: 8px; border-radius: 50%; background: #78d6ac; box-shadow: 0 0 0 3px rgba(120,214,172,.15); }
.grid {
    display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; padding: 11px 13px 8px;
}
.stat { background: var(--soft); border: 1px solid var(--line); border-radius: 8px; padding: 9px 10px; min-width: 0; }
.value {
    font-size: 18px; font-weight: 850; margin-top: 4px; color: var(--ink);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.description {
    margin: 0 13px 9px; padding: 9px 11px; background: #f8fafc; border: 1px solid var(--line);
    border-left: 4px solid var(--blue); border-radius: 8px; color: #455363; font-size: 12px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.operation-history { margin: 0 13px 10px; }
.operation-history-heading {
    display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap;
    margin: 0 1px 6px;
}
.operation-history-title { font-size: 12px; font-weight: 900; color: var(--navy); text-transform: uppercase; letter-spacing: .45px; }
.operation-history-note { font-size: 10px; color: var(--muted); }
.history-wrap { display: grid; grid-template-columns: 1.15fr 1fr; gap: 8px; }
.history-card { border: 1px solid var(--line); border-radius: 9px; padding: 8px; background: #fbfcfe; min-width: 0; }
.history-card.production { border-top: 4px solid var(--blue); }
.history-card.setup { border-top: 4px solid #8a97a5; }
.history-title { font-size: 11px; font-weight: 900; color: var(--navy); margin-bottom: 6px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.history-title small { color: var(--muted); font-weight: 700; font-size: 9px; }
.history-stats { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 5px; }
.hist-stat { background: white; border: 1px solid var(--line); border-radius: 7px; padding: 6px 5px; text-align: center; min-width: 0; }
.hist-value { font-size: 15px; font-weight: 900; margin-top: 2px; color: var(--navy); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hist-stat.eff-good { background: var(--good-bg); border-color: var(--good-line); }
.hist-stat.eff-good .hist-value { color: var(--good-text); }
.hist-stat.eff-bad { background: var(--bad-bg); border-color: var(--bad-line); }
.hist-stat.eff-bad .hist-value { color: var(--bad-text); }
.no-history { padding: 8px; color: var(--muted); font-size: 11px; text-align: center; background: white; border: 1px dashed var(--line); border-radius: 7px; }
.live-box {
    margin: 0 13px 11px; padding: 12px; border: 1px solid var(--line); border-radius: 10px; background: #fbfcfe;
}
.performance-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr); gap: 11px; align-items: stretch; }
.performance-left { display: grid; grid-template-rows: auto auto 1fr; gap: 9px; min-width: 0; }
.live-input-row { display: flex; align-items: end; gap: 9px; flex-wrap: nowrap; }
.live-input-group { min-width: 0; flex: 1 1 auto; }
.live-input-group label { display: block; font-size: 12px; font-weight: 850; margin-bottom: 5px; color: var(--navy); }
.qty-input { width: 100%; max-width: 245px; font-size: 22px; font-weight: 750; min-height: 44px; }
.result-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.result-card {
    background: white; border: 1px solid var(--line); border-radius: 8px; padding: 9px; text-align: center; min-width: 0;
}
.result-value { font-size: 24px; font-weight: 850; margin-top: 3px; color: var(--navy); }
.efficiency-box {
    text-align: center; padding: 12px 13px; border-radius: 9px;
    background: linear-gradient(135deg, #eaf2fb, #f5f9fd); border: 1px solid #c9dcef;
    display: flex; align-items: center; justify-content: center; gap: 18px; min-height: 88px;
}
.efficiency-label { font-size: 11px; font-weight: 850; color: var(--blue); text-transform: uppercase; letter-spacing: .85px; }
.efficiency-value { font-size: 46px; line-height: 1; font-weight: 900; color: var(--navy); letter-spacing: -1px; }
.projection-box {
    padding: 13px; border-radius: 9px; border: 1px solid var(--line); background: white;
    display: flex; flex-direction: column; justify-content: center; min-width: 0;
}
.projection-box.status-neutral { background: #f8fafc; }
.projection-box.status-ontrack { background: #edf8f2; border-color: #abd9c3; }
.projection-box.status-over { background: #fff1f1; border-color: #e5b3b3; }
.projection-status { font-size: 21px; line-height: 1.18; font-weight: 900; color: var(--navy); margin-top: 4px; }
.projection-box.status-ontrack .projection-status { color: #17633f; }
.projection-box.status-over .projection-status { color: #9b2f2f; }
.projection-detail { margin-top: 5px; color: var(--muted); line-height: 1.35; font-size: 12px; }
.projection-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 9px; }
.projection-stat { background: rgba(255,255,255,.78); border: 1px solid rgba(0,0,0,.07); border-radius: 8px; padding: 9px; text-align: center; }
.projection-stat .result-value { font-size: 21px; }
.small-note { font-size: 11px; color: var(--muted); margin-top: 7px; line-height: 1.35; }
.setup-box {
    margin: 0 13px 11px; padding: 16px; border: 1px solid var(--line); border-radius: 10px;
    text-align: center; background: linear-gradient(135deg, #f7f9fb, #ffffff);
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; align-items: center;
}
.setup-value { font-size: 33px; font-weight: 850; color: var(--navy); }
.footer { color: #7a8794; text-align: center; font-size: 11px; padding: 7px 0 10px; }

@media (max-width: 1180px) {
    .grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .performance-layout { grid-template-columns: 1fr; }
    .projection-box { min-height: 0; }
}
@media (max-width: 760px) {
    .topbar { align-items: stretch; flex-direction: column; padding: 11px 14px; }
    .nav { width: 100%; }
    .nav a { flex: 1 1 140px; min-width: 0; font-size: 13px; padding: 8px 7px; }
    .shell { padding: 12px; }
    .page-hero { display: block; }
    .subtitle { margin-top: 5px; }
    .search-box { align-items: stretch; }
    .field-group, .search-box input, .search-box button { width: 100%; }
    .job-header { align-items: flex-start; }
    .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .result-grid, .projection-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .live-input-row { flex-wrap: wrap; }
    .qty-input { max-width: none; }
    .live-input-row button { width: 100%; }
    .efficiency-box { flex-direction: column; gap: 5px; }
    .setup-box { grid-template-columns: 1fr; }
    .history-wrap { grid-template-columns: 1fr; }
    .history-stats { grid-template-columns: repeat(5, minmax(70px, 1fr)); overflow-x: auto; }
}
@media (max-width: 520px) {
    h1 { font-size: 23px; }
    .grid, .result-grid, .projection-grid { grid-template-columns: 1fr 1fr; }
    .job-title { font-size: 18px; }
    .efficiency-value { font-size: 40px; }
    .projection-status { font-size: 18px; }
}
</style>
</head>
<body>
<header class="topbar">
    <div class="brand"><span class="brand-mark">M2M</span><span>Labor Analytics</span></div>
    <nav class="nav">
        <a href="/">Part Labor History</a>
        <a class="active" href="/live">Live Employee Performance</a>
        <a href="/dashboard">Live Job Dashboard</a>
        <a href="/completed">Completed Job Review</a>
    </nav>
</header>
<main class="shell">
    <section class="page-hero">
        <div class="eyebrow">Shop Floor Dashboard</div>
        <h1>Live Employee Performance</h1>
        <div class="subtitle">Active M2M job, current efficiency, and projected finish status in one compact view.</div>
    </section>
    <div class="search-box">
        <div class="field-group">
            <label class="field-label" for="employeeNumber">Employee Number</label>
            <input id="employeeNumber" type="text" placeholder="Example: 135">
        </div>
        <button onclick="loadEmployee()">Find Current Job</button>
    </div>
    <div id="results"></div>
    <div class="footer">Made2Manage labor analytics • Internal use</div>
</main>
<script>
let liveJobs = [];
let selectedJobIndex = 0;

function safeNumber(value) {
    const n = Number(value);
    return isNaN(n) ? 0 : n;
}

function formatDateTime(value) {
    if (!value) return "";
    const d = new Date(value);
    if (isNaN(d.getTime())) return value;
    return d.toLocaleString();
}

function efficiencyClass(value) {
    return safeNumber(value) >= 85 ? 'eff-good' : 'eff-bad';
}

function historyCard(summary, type) {
    const runs = safeNumber(summary?.Runs);
    if (!runs) {
        return `<div class="history-card ${type.toLowerCase()}">
            <div class="history-title">${type} History <small>No prior history</small></div>
            <div class="no-history">No comparable ${type.toLowerCase()} history for this part revision and operation.</div>
        </div>`;
    }

    const adjustedRuns = safeNumber(summary.AdjustedRuns);
    const excluded = safeNumber(summary.ExcludedRuns);
    const avgQty = safeNumber(summary.AvgQuantity);
    const avgActual = safeNumber(summary.AvgActualHours);
    const avgEstimated = safeNumber(summary.AvgEstimatedHours);
    const efficiency = safeNumber(summary.WeightedEfficiency);
    const rawEfficiency = safeNumber(summary.RawWeightedEfficiency);
    const quantityBlock = type === 'Production'
        ? `<div class="hist-stat"><div class="label">Avg Qty</div><div class="hist-value">${avgQty.toFixed(1)}</div></div>`
        : `<div class="hist-stat"><div class="label">Jobs</div><div class="hist-value">${safeNumber(summary.Jobs).toFixed(0)}</div></div>`;

    return `<div class="history-card ${type.toLowerCase()}">
        <div class="history-title">${type} History <small>${adjustedRuns.toFixed(0)} of ${runs.toFixed(0)} used • ${excluded.toFixed(0)} excluded</small></div>
        <div class="history-stats">
            <div class="hist-stat"><div class="label">Runs</div><div class="hist-value">${runs.toFixed(0)}</div></div>
            ${quantityBlock}
            <div class="hist-stat"><div class="label">Avg Actual</div><div class="hist-value">${avgActual.toFixed(2)}h</div></div>
            <div class="hist-stat"><div class="label">Avg Est.</div><div class="hist-value">${avgEstimated.toFixed(2)}h</div></div>
            <div class="hist-stat ${efficiencyClass(efficiency)}"><div class="label">Adjusted Eff.</div><div class="hist-value">${efficiency.toFixed(1)}%</div></div>
            <div class="hist-stat"><div class="label">Raw Eff.</div><div class="hist-value">${rawEfficiency.toFixed(1)}%</div></div>
        </div>
        <div class="no-history" style="margin-top:6px">
            ${safeNumber(summary.Below80Runs).toFixed(0)} below 80% •
            ${safeNumber(summary.HighReviewRuns).toFixed(0)} high review •
            ${safeNumber(summary.StatisticalOutliers).toFixed(0)} stat outlier •
            ${safeNumber(summary.DataReviewRuns).toFixed(0)} data review
        </div>
    </div>`;
}

function renderOperationHistory(job) {
    return `<div class="operation-history">
        <div class="operation-history-heading">
            <div class="operation-history-title">Current Operation Average Labor History</div>
            <div class="operation-history-note">Same part revision + operation • adjusted average excludes MAD statistical outliers and invalid-hour records</div>
        </div>
        <div class="history-wrap">
            ${historyCard(job.HistoricalProduction || {}, 'Production')}
            ${historyCard(job.HistoricalSetup || {}, 'Setup')}
        </div>
    </div>`;
}

async function loadEmployee() {
    const employeeNumber = document.getElementById("employeeNumber").value.trim();
    const results = document.getElementById("results");
    if (!employeeNumber) {
        results.innerHTML = '<div class="error">Enter an employee number.</div>';
        return;
    }
    results.innerHTML = '<div class="loading">Looking up current Made2Manage labor...</div>';
    try {
        const response = await fetch('/api/live/' + encodeURIComponent(employeeNumber));
        if (!response.ok) {
            const detail = await response.json().catch(() => ({}));
            throw new Error(detail.detail || 'No active direct labor job found.');
        }
        liveJobs = await response.json();
        selectedJobIndex = 0;
        renderJobs();
    } catch (error) {
        results.innerHTML = '<div class="error">' + error.message + '</div>';
    }
}

function selectJob(index) {
    selectedJobIndex = index;
    renderJobs();
}

function renderJobs() {
    const results = document.getElementById("results");
    if (!liveJobs.length) {
        results.innerHTML = '<div class="error">No active direct labor job found.</div>';
        return;
    }

    let html = "";
    if (liveJobs.length > 1) {
        html += '<div class="job-selector"><span class="job-selector-label">ACTIVE JOBS:</span>';
        liveJobs.forEach((item, i) => {
            const selected = i === selectedJobIndex ? ' selected' : '';
            html += `<button class="${selected}" onclick="selectJob(${i})">${item.JobNumber} • Op ${item.OperationNumber}</button>`;
        });
        html += '</div>';
    }

    const index = Math.min(selectedJobIndex, liveJobs.length - 1);
    const job = liveJobs[index];
    const opQty = safeNumber(job.OperationQuantity);
    const reported = safeNumber(job.OperationQtyReported);
    const remaining = safeNumber(job.OperationQtyRemaining);
    const remainingEstimate = safeNumber(job.RemainingProductionEstimate);
    const setupEstimate = safeNumber(job.SetupEstimate);
    const targetRate = safeNumber(job.TargetPiecesPerHour);
    const elapsed = safeNumber(job.CurrentSegmentHours);
    const laborType = job.ActiveLaborType || "Production";
    const operationText = job.OperationDescription || '';

    html += `<div class="job-card">
        <div class="job-header">
            <div>
                <div class="job-title">${job.EmployeeName || ''} (${job.EmployeeNumber})</div>
                <div class="job-subtitle">Job ${job.JobNumber} &nbsp; • &nbsp; Part ${job.PartNumber || ''} ${job.Revision ? 'Rev ' + job.Revision : ''} &nbsp; • &nbsp; Operation ${job.OperationNumber}</div>
            </div>
            <div class="live-pill"><span class="live-dot"></span>LIVE</div>
        </div>
        <div class="grid">
            <div class="stat"><div class="label">Labor Type</div><div class="value">${laborType}</div></div>
            <div class="stat"><div class="label">Work Center</div><div class="value">${job.WorkCenter || ''}</div></div>
            <div class="stat"><div class="label">Clocked In</div><div class="value" style="font-size:12px" title="${formatDateTime(job.ClockInTime)}">${formatDateTime(job.ClockInTime)}</div></div>
            <div class="stat"><div class="label">Segment Hrs</div><div class="value" id="elapsed-${index}">${elapsed.toFixed(2)}</div></div>
            <div class="stat"><div class="label">Operation Qty</div><div class="value">${opQty.toFixed(0)}</div></div>
            <div class="stat"><div class="label">M2M Complete</div><div class="value">${reported.toFixed(0)}</div></div>
            <div class="stat"><div class="label">M2M Remaining</div><div class="value">${remaining.toFixed(0)}</div></div>
            <div class="stat"><div class="label">Remaining Est.</div><div class="value">${remainingEstimate.toFixed(2)} hrs</div></div>
        </div>
        ${operationText ? `<div class="description" title="${operationText.replace(/"/g, '&quot;')}"><strong>Operation:</strong> ${operationText}</div>` : ''}
        ${renderOperationHistory(job)}
        ${laborType === 'Setup' ? renderSetup(job, index, setupEstimate, elapsed) : renderProduction(job, index, targetRate, elapsed)}
    </div>`;

    results.innerHTML = html;
}

function renderSetup(job, index, setupEstimate, elapsed) {
    let budgetUsed = setupEstimate > 0 ? (elapsed / setupEstimate) * 100 : 0;
    return `<div class="setup-box">
        <div><div class="label">Setup Estimate</div><div class="setup-value">${setupEstimate.toFixed(2)} hrs</div></div>
        <div><div class="label">Current Setup Time</div><div class="setup-value">${elapsed.toFixed(2)} hrs</div></div>
        <div><div class="label">Setup Budget Used</div><div class="setup-value">${budgetUsed.toFixed(1)}%</div></div>
    </div>`;
}

function renderProduction(job, index, targetRate, elapsed) {
    return `<div class="live-box">
        <div class="performance-layout">
            <div class="performance-left">
                <div class="live-input-row">
                    <div class="live-input-group">
                        <label>Pieces completed since this clock-on</label>
                        <input class="qty-input" id="qty-${index}" type="number" min="0" step="1" value="0" oninput="calculateEfficiency(${index})">
                    </div>
                    <button onclick="calculateEfficiency(${index})">Calculate</button>
                </div>
                <div class="result-grid">
                    <div class="result-card"><div class="label">Target Rate</div><div class="result-value">${targetRate.toFixed(2)}</div><div class="label">pcs/hr</div></div>
                    <div class="result-card"><div class="label">Actual Rate</div><div class="result-value" id="actual-rate-${index}">0.00</div><div class="label">pcs/hr</div></div>
                    <div class="result-card"><div class="label">Earned Hours</div><div class="result-value" id="earned-${index}">0.00</div></div>
                </div>
                <div class="efficiency-box">
                    <div class="efficiency-label">Current Labor Efficiency</div>
                    <div class="efficiency-value" id="efficiency-${index}">--</div>
                </div>
            </div>
            <div class="projection-box status-neutral" id="projection-box-${index}">
                <div class="label">Production Finish Projection</div>
                <div class="projection-status" id="projection-status-${index}">Enter current quantity to project finish</div>
                <div class="projection-detail" id="projection-detail-${index}">Compares the current segment pace with M2M's estimated production time for the quantity M2M currently shows remaining.</div>
                <div class="projection-grid">
                    <div class="projection-stat"><div class="label">Projected Hrs</div><div class="result-value" id="projected-hours-${index}">--</div></div>
                    <div class="projection-stat"><div class="label">M2M Est. Hrs</div><div class="result-value" id="remaining-estimate-${index}">${safeNumber(job.RemainingProductionEstimate).toFixed(2)}</div></div>
                    <div class="projection-stat"><div class="label">Variance</div><div class="result-value" id="projected-variance-${index}">--</div></div>
                </div>
                <div class="small-note">Projection assumes the current pace continues. Entered quantity is not saved to M2M.</div>
            </div>
        </div>
    </div>`;
}

async function calculateEfficiency(index) {
    const job = liveJobs[index];
    if (!job || job.ActiveLaborType === 'Setup') return;

    const qtyInput = document.getElementById('qty-' + index);
    if (!qtyInput) return;
    const qty = Math.max(0, Number(qtyInput.value) || 0);
    const earnedElement = document.getElementById('earned-' + index);
    const actualRateElement = document.getElementById('actual-rate-' + index);
    const efficiencyElement = document.getElementById('efficiency-' + index);
    const projectionBox = document.getElementById('projection-box-' + index);
    const projectionStatus = document.getElementById('projection-status-' + index);
    const projectionDetail = document.getElementById('projection-detail-' + index);
    const projectedHoursElement = document.getElementById('projected-hours-' + index);
    const projectedVarianceElement = document.getElementById('projected-variance-' + index);

    const elapsed = safeNumber(job.CurrentSegmentHours);
    const m2mRemainingQty = safeNumber(job.OperationQtyRemaining);
    const m2mRemainingEstimate = safeNumber(job.RemainingProductionEstimate);

    if (actualRateElement) {
        actualRateElement.textContent = elapsed > 0 ? (qty / elapsed).toFixed(2) : '--';
    }

    function resetProjection(message) {
        projectionBox.className = 'projection-box status-neutral';
        projectionStatus.textContent = message;
        projectionDetail.textContent = "Compares the current segment pace with M2M's estimated production time for the quantity M2M currently shows remaining.";
        projectedHoursElement.textContent = '--';
        projectedVarianceElement.textContent = '--';
    }

    if (qty <= 0) {
        earnedElement.textContent = '0.00';
        efficiencyElement.textContent = '--';
        resetProjection('Enter current quantity to project finish');
        return;
    }

    if (elapsed <= 0) {
        earnedElement.textContent = '0.00';
        efficiencyElement.textContent = '--';
        resetProjection('Not enough elapsed time to project finish');
        return;
    }

    try {
        const url = '/api/live-estimate?job_number=' + encodeURIComponent(job.JobNumber)
            + '&operation_number=' + encodeURIComponent(job.OperationNumber)
            + '&quantity=' + encodeURIComponent(qty);
        const response = await fetch(url);
        if (!response.ok) throw new Error('Could not calculate M2M estimate.');
        const result = await response.json();
        const earned = safeNumber(result.EarnedHours);
        const efficiency = (earned / elapsed) * 100;

        earnedElement.textContent = earned.toFixed(2);
        efficiencyElement.textContent = efficiency.toFixed(1) + '%';

        if (m2mRemainingQty <= 0 || m2mRemainingEstimate <= 0) {
            resetProjection('M2M does not have a usable remaining production estimate');
            return;
        }

        const currentRate = qty / elapsed;
        const qtyStillToProduce = Math.max(m2mRemainingQty - qty, 0);
        const additionalHours = currentRate > 0 ? qtyStillToProduce / currentRate : 0;
        const projectedProductionHours = elapsed + additionalHours;
        const projectedVariance = projectedProductionHours - m2mRemainingEstimate;

        projectedHoursElement.textContent = projectedProductionHours.toFixed(2);
        projectedVarianceElement.textContent = (projectedVariance >= 0 ? '+' : '') + projectedVariance.toFixed(2) + ' hrs';

        if (projectedVariance <= 0) {
            projectionBox.className = 'projection-box status-ontrack';
            projectionStatus.textContent = 'ON TRACK TO FINISH WITHIN ESTIMATE';
            projectionDetail.textContent = 'Projected to finish about ' + Math.abs(projectedVariance).toFixed(2) + ' hours under the M2M remaining production estimate.';
        } else {
            projectionBox.className = 'projection-box status-over';
            projectionStatus.textContent = 'PROJECTED TO EXCEED ESTIMATED TIME';
            projectionDetail.textContent = 'Projected to finish about ' + projectedVariance.toFixed(2) + ' hours over the M2M remaining production estimate.';
        }
    } catch (error) {
        earnedElement.textContent = 'ERR';
        efficiencyElement.textContent = 'ERR';
        projectionBox.className = 'projection-box status-neutral';
        projectionStatus.textContent = 'Projection unavailable';
        projectionDetail.textContent = error.message;
        projectedHoursElement.textContent = 'ERR';
        projectedVarianceElement.textContent = 'ERR';
    }
}

document.getElementById("employeeNumber").addEventListener("keydown", function(event) {
    if (event.key === "Enter") loadEmployee();
});
</script>
</body>
</html>"""


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live Job Dashboard</title>
<style>
:root {
    --navy: #14273d;
    --navy-2: #1f3a56;
    --blue: #2767a8;
    --ink: #1c2733;
    --muted: #667587;
    --line: #d9e1e8;
    --panel: #ffffff;
    --canvas: #eef2f6;
    --soft: #f7f9fb;
    --good-bg: #edf8f2;
    --good-line: #abd9c3;
    --good-text: #17633f;
    --bad-bg: #fff1f1;
    --bad-line: #e5b3b3;
    --bad-text: #9b2f2f;
    --shadow: 0 7px 20px rgba(22, 38, 55, .08);
}
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", Arial, sans-serif; background: var(--canvas); color: var(--ink); }
.topbar {
    background: linear-gradient(135deg, var(--navy), var(--navy-2)); color: white;
    min-height: 64px; display: flex; align-items: center; justify-content: space-between;
    gap: 18px; padding: 9px 24px; box-shadow: 0 3px 11px rgba(0,0,0,.14);
}
.brand { display: flex; align-items: center; gap: 11px; font-size: 19px; font-weight: 750; }
.brand-mark { width: 38px; height: 38px; border-radius: 9px; display: inline-flex; align-items: center; justify-content: center; background: white; color: var(--navy); font-size: 12px; font-weight: 850; }
.nav { display: flex; gap: 6px; flex-wrap: wrap; padding: 5px; background: rgba(255,255,255,.10); border: 1px solid rgba(255,255,255,.20); border-radius: 11px; }
.nav a { text-decoration: none; color: #e5edf6; padding: 9px 15px; border-radius: 8px; font-weight: 800; font-size: 13px; min-width: 168px; text-align: center; border: 1px solid transparent; }
.nav a:hover { background: rgba(255,255,255,.14); color: white; }
.nav a.active { background: white; color: var(--navy); border-color: white; box-shadow: 0 3px 9px rgba(0,0,0,.15); }
.nav a.active::after, .nav a:not(.active)::after { display: block; margin-top: 2px; font-size: 8px; letter-spacing: .9px; }
.nav a.active::after { content: "CURRENT VIEW"; color: var(--blue); }
.nav a:not(.active)::after { content: "SWITCH VIEW"; color: #b9cada; }
.shell { max-width: 1650px; margin: 0 auto; padding: 16px 22px 12px; }
.page-hero { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; margin-bottom: 12px; }
.eyebrow { text-transform: uppercase; letter-spacing: 1.2px; color: var(--blue); font-size: 11px; font-weight: 850; }
h1 { margin: 0; font-size: 28px; letter-spacing: -.35px; }
.subtitle { color: var(--muted); font-size: 13px; margin: 0; line-height: 1.4; flex: 1 1 600px; }
.toolbar { background: white; border: 1px solid var(--line); border-radius: 11px; padding: 11px 13px; box-shadow: var(--shadow); display: flex; align-items: end; gap: 10px; flex-wrap: wrap; margin-bottom: 11px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.label { font-size: 10px; font-weight: 850; text-transform: uppercase; letter-spacing: .55px; color: var(--muted); }
input, select, button { font: inherit; min-height: 39px; border-radius: 8px; }
input, select { padding: 8px 10px; border: 1px solid #c8d2dc; background: white; color: var(--ink); min-width: 190px; }
button { padding: 8px 14px; cursor: pointer; border: 1px solid var(--blue); background: var(--blue); color: white; font-weight: 800; }
button:hover { filter: brightness(.96); }
.auto-note { margin-left: auto; align-self: center; font-size: 11px; color: var(--muted); }
.summary-grid { display: grid; grid-template-columns: repeat(5, minmax(130px, 1fr)); gap: 9px; margin-bottom: 11px; }
.summary-card { background: white; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; box-shadow: var(--shadow); }
.summary-value { font-size: 24px; font-weight: 900; color: var(--navy); margin-top: 3px; }
.status { background: white; border: 1px solid var(--line); border-left: 4px solid var(--blue); border-radius: 9px; padding: 11px 13px; margin-bottom: 10px; box-shadow: var(--shadow); font-size: 13px; }
.jobs-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; }
.job-card { background: white; border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); overflow: hidden; min-width: 0; }
.job-header { background: linear-gradient(135deg, var(--navy), var(--navy-2)); color: white; padding: 11px 14px; display: flex; justify-content: space-between; gap: 12px; align-items: center; }
.employee { font-size: 19px; font-weight: 900; }
.job-line { margin-top: 3px; font-size: 12px; color: #d9e5ef; line-height: 1.35; }
.live-pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 9px; border-radius: 999px; border: 1px solid rgba(255,255,255,.30); background: rgba(255,255,255,.10); font-size: 10px; font-weight: 850; letter-spacing: .65px; white-space: nowrap; }
.live-dot { width: 8px; height: 8px; border-radius: 50%; background: #78d6ac; box-shadow: 0 0 0 3px rgba(120,214,172,.15); }
.job-meta { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; padding: 9px 11px 7px; }
.meta { background: var(--soft); border: 1px solid var(--line); border-radius: 8px; padding: 8px; min-width: 0; }
.meta-value { margin-top: 3px; font-size: 14px; font-weight: 850; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.description { margin: 0 11px 8px; padding: 7px 9px; background: #f8fafc; border: 1px solid var(--line); border-left: 4px solid var(--blue); border-radius: 7px; font-size: 11px; color: #455363; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.history-wrap { padding: 0 11px 11px; display: grid; grid-template-columns: 1.15fr 1fr; gap: 8px; }
.history-card { border: 1px solid var(--line); border-radius: 9px; padding: 9px; background: #fbfcfe; min-width: 0; }
.history-card.production { border-top: 4px solid var(--blue); }
.history-card.setup { border-top: 4px solid #8a97a5; }
.history-title { font-size: 12px; font-weight: 900; color: var(--navy); margin-bottom: 7px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.history-title small { color: var(--muted); font-weight: 700; }
.history-stats { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 5px; }
.hist-stat { background: white; border: 1px solid var(--line); border-radius: 7px; padding: 6px; text-align: center; min-width: 0; }
.hist-value { font-size: 15px; font-weight: 900; margin-top: 2px; color: var(--navy); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hist-stat.eff-good { background: var(--good-bg); border-color: var(--good-line); }
.hist-stat.eff-good .hist-value { color: var(--good-text); }
.hist-stat.eff-bad { background: var(--bad-bg); border-color: var(--bad-line); }
.hist-stat.eff-bad .hist-value { color: var(--bad-text); }
.no-history { padding: 10px; color: var(--muted); font-size: 12px; text-align: center; background: white; border: 1px dashed var(--line); border-radius: 7px; }
.footer { color: #7a8794; text-align: center; font-size: 11px; padding: 10px 0; }
@media (max-width: 1150px) { .jobs-grid { grid-template-columns: 1fr; } .history-wrap { grid-template-columns: 1fr 1fr; } }
@media (max-width: 800px) {
    .jobs-table-wrap { overflow-x: auto; }
    .topbar { align-items: stretch; flex-direction: column; padding: 11px 14px; }
    .nav { width: 100%; }
    .nav a { flex: 1 1 140px; min-width: 0; font-size: 12px; padding: 8px 6px; }
    .shell { padding: 12px; }
    .summary-grid { grid-template-columns: repeat(2, 1fr); }
    .job-meta { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .history-wrap { grid-template-columns: 1fr; }
    .history-stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .toolbar .field, .toolbar input, .toolbar select, .toolbar button { width: 100%; }
    .auto-note { margin-left: 0; }
}
</style>
</head>
<body>
<header class="topbar">
    <div class="brand"><span class="brand-mark">M2M</span><span>M2M Labor Analytics</span></div>
    <nav class="nav">
        <a href="/">Part Labor History</a>
        <a href="/live">Live Employee Performance</a>
        <a class="active" href="/dashboard">Live Job Dashboard</a>
        <a href="/completed">Completed Job Review</a>
    </nav>
</header>
<main class="shell">
    <div class="page-hero">
        <div><div class="eyebrow">Shop Floor Overview</div><h1>Live Job Dashboard</h1></div>
        <p class="subtitle">All employees currently clocked onto direct labor, paired with the adjusted historical Setup and Production averages for the same part revision and operation.</p>
    </div>
    <div class="toolbar">
        <div class="field"><label class="label" for="employeeFilter">Employee / Job / Part</label><input id="employeeFilter" type="text" placeholder="Filter dashboard" oninput="applyDashboardFilters()"></div>
        <div class="field"><label class="label" for="workCenterFilter">Work Center</label><select id="workCenterFilter" onchange="applyDashboardFilters()"><option value="">All Work Centers</option></select></div>
        <div class="field"><label class="label" for="typeFilter">Active Labor Type</label><select id="typeFilter" onchange="applyDashboardFilters()"><option value="">All Labor</option><option value="Setup">Setup</option><option value="Production">Production</option></select></div>
        <button onclick="loadDashboard(true)">Refresh Now</button>
        <div class="auto-note">Automatically refreshes every 30 seconds.</div>
    </div>
    <div id="summaryArea"></div>
    <div id="statusArea" class="status">Loading live jobs...</div>
    <div id="jobsGrid" class="jobs-grid"></div>
    <div class="footer">Historical adjusted averages exclude MAD statistical outliers and invalid-hour records • Internal use</div>
</main>
<script>
let dashboardJobs = [];
let refreshTimer = null;

function safeNumber(value) { const n = Number(value); return Number.isFinite(n) ? n : 0; }
function escapeHtml(value) { return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }
function formatDateTime(value) {
    if (!value) return '';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value).replace('T', ' ');
    return d.toLocaleString([], {month:'numeric', day:'numeric', hour:'numeric', minute:'2-digit'});
}
function efficiencyClass(value) { return safeNumber(value) >= 85 ? 'eff-good' : 'eff-bad'; }

async function loadDashboard(showLoading=false) {
    const status = document.getElementById('statusArea');
    if (showLoading || !dashboardJobs.length) {
        status.style.display = '';
        status.textContent = 'Loading live jobs...';
    }
    try {
        const response = await fetch('/api/live-dashboard', {cache:'no-store'});
        if (!response.ok) throw new Error('Unable to load live job dashboard.');
        const data = await response.json();
        dashboardJobs = Array.isArray(data.jobs) ? data.jobs : [];
        rebuildWorkCenterFilter();
        renderSummary();
        applyDashboardFilters();
        status.style.display = 'none';
    } catch (error) {
        status.style.display = '';
        status.textContent = error.message;
    }
}

function rebuildWorkCenterFilter() {
    const select = document.getElementById('workCenterFilter');
    const current = select.value;
    const centers = [...new Set(dashboardJobs.map(job => {
        const name = String(job.WorkCenterName || '').trim();
        const code = String(job.WorkCenter || '').trim();
        return name ? `${name} (WC ${code})` : (code ? `Work Center ${code}` : '');
    }).filter(Boolean))].sort((a,b) => a.localeCompare(b, undefined, {numeric:true}));
    select.innerHTML = '<option value="">All Work Centers</option>' + centers.map(center => `<option value="${escapeHtml(center)}">${escapeHtml(center)}</option>`).join('');
    if (centers.includes(current)) select.value = current;
}

function renderSummary() {
    const uniqueEmployees = new Set(dashboardJobs.map(job => job.EmployeeNumber)).size;
    const setup = dashboardJobs.filter(job => job.ActiveLaborType === 'Setup').length;
    const production = dashboardJobs.filter(job => job.ActiveLaborType === 'Production').length;
    const now = new Date().toLocaleTimeString([], {hour:'numeric', minute:'2-digit', second:'2-digit'});
    document.getElementById('summaryArea').innerHTML = `<div class="summary-grid">
        <div class="summary-card"><div class="label">Active Jobs</div><div class="summary-value">${dashboardJobs.length}</div></div>
        <div class="summary-card"><div class="label">Employees</div><div class="summary-value">${uniqueEmployees}</div></div>
        <div class="summary-card"><div class="label">Production</div><div class="summary-value">${production}</div></div>
        <div class="summary-card"><div class="label">Setup</div><div class="summary-value">${setup}</div></div>
        <div class="summary-card"><div class="label">Last Refresh</div><div class="summary-value" style="font-size:18px">${now}</div></div>
    </div>`;
}

function workCenterLabel(job) {
    const code = String(job.WorkCenter || '').trim();
    const name = String(job.WorkCenterName || '').trim();
    if (name && code) return `${name} (WC ${code})`;
    if (name) return name;
    if (code) return `Work Center ${code}`;
    return '';
}

function applyDashboardFilters() {
    const text = document.getElementById('employeeFilter').value.trim().toLowerCase();
    const wc = document.getElementById('workCenterFilter').value;
    const type = document.getElementById('typeFilter').value;
    const filtered = dashboardJobs.filter(job => {
        const haystack = [job.EmployeeName, job.EmployeeNumber, job.JobNumber, job.PartNumber, job.Revision, job.OperationNumber, workCenterLabel(job)].join(' ').toLowerCase();
        return (!text || haystack.includes(text)) && (!wc || workCenterLabel(job) === wc) && (!type || job.ActiveLaborType === type);
    });
    renderJobs(filtered);
}

function historyCard(summary, type) {
    const runs = safeNumber(summary?.Runs);
    if (!runs) return `<div class="history-card ${type.toLowerCase()}"><div class="history-title">${type} History <small>No prior history</small></div><div class="no-history">No comparable ${type.toLowerCase()} labor history for this part revision and operation.</div></div>`;
    const adjustedRuns = safeNumber(summary.AdjustedRuns);
    const excluded = safeNumber(summary.ExcludedRuns);
    const avgQty = safeNumber(summary.AvgQuantity);
    const avgActual = safeNumber(summary.AvgActualHours);
    const avgEstimated = safeNumber(summary.AvgEstimatedHours);
    const efficiency = safeNumber(summary.WeightedEfficiency);
    const rawEfficiency = safeNumber(summary.RawWeightedEfficiency);
    const quantityBlock = type === 'Production'
        ? `<div class="hist-stat"><div class="label">Avg Qty</div><div class="hist-value">${avgQty.toFixed(1)}</div></div>`
        : `<div class="hist-stat"><div class="label">Jobs</div><div class="hist-value">${safeNumber(summary.Jobs).toFixed(0)}</div></div>`;
    return `<div class="history-card ${type.toLowerCase()}">
        <div class="history-title">${type} History <small>${adjustedRuns.toFixed(0)} of ${runs.toFixed(0)} used • ${excluded.toFixed(0)} excluded</small></div>
        <div class="history-stats">
            <div class="hist-stat"><div class="label">Runs</div><div class="hist-value">${runs.toFixed(0)}</div></div>
            ${quantityBlock}
            <div class="hist-stat"><div class="label">Avg Actual</div><div class="hist-value">${avgActual.toFixed(2)}h</div></div>
            <div class="hist-stat"><div class="label">Avg Est.</div><div class="hist-value">${avgEstimated.toFixed(2)}h</div></div>
            <div class="hist-stat ${efficiencyClass(efficiency)}"><div class="label">Adjusted Eff.</div><div class="hist-value">${efficiency.toFixed(1)}%</div></div>
            <div class="hist-stat"><div class="label">Raw Eff.</div><div class="hist-value">${rawEfficiency.toFixed(1)}%</div></div>
        </div>
        <div class="no-history" style="margin-top:6px">${safeNumber(summary.Below80Runs).toFixed(0)} below 80% • ${safeNumber(summary.HighReviewRuns).toFixed(0)} high review • ${safeNumber(summary.StatisticalOutliers).toFixed(0)} stat outlier • ${safeNumber(summary.DataReviewRuns).toFixed(0)} data review</div>
    </div>`;
}

function renderJobs(jobs) {
    const grid = document.getElementById('jobsGrid');
    const status = document.getElementById('statusArea');
    if (!dashboardJobs.length) {
        status.style.display = '';
        status.textContent = 'No active direct labor jobs are currently clocked on.';
        grid.innerHTML = '';
        return;
    }
    if (!jobs.length) {
        status.style.display = '';
        status.textContent = 'No active jobs match the selected filters.';
        grid.innerHTML = '';
        return;
    }
    status.style.display = 'none';
    grid.innerHTML = jobs.map(job => {
        const employee = escapeHtml(job.EmployeeName || ('Employee ' + (job.EmployeeNumber || '')));
        const wc = escapeHtml(workCenterLabel(job));
        const description = escapeHtml(job.OperationDescription || '');
        const activeType = escapeHtml(job.ActiveLaborType || job.ActiveTypeCode || '');
        return `<section class="job-card">
            <div class="job-header">
                <div><div class="employee">${employee} (${escapeHtml(job.EmployeeNumber)})</div><div class="job-line">Job ${escapeHtml(job.JobNumber)} • Part ${escapeHtml(job.PartNumber)} ${job.Revision ? 'Rev ' + escapeHtml(job.Revision) : ''} • Operation ${escapeHtml(job.OperationNumber)}</div></div>
                <div class="live-pill"><span class="live-dot"></span>LIVE</div>
            </div>
            <div class="job-meta">
                <div class="meta"><div class="label">Active Labor</div><div class="meta-value">${activeType}</div></div>
                <div class="meta"><div class="label">Work Center</div><div class="meta-value" title="${wc}">${wc || '—'}</div></div>
                <div class="meta"><div class="label">Clocked On</div><div class="meta-value">${escapeHtml(formatDateTime(job.ClockInTime))}</div></div>
                <div class="meta"><div class="label">Segment Hrs</div><div class="meta-value">${safeNumber(job.CurrentSegmentHours).toFixed(2)}</div></div>
                <div class="meta"><div class="label">Operation Qty</div><div class="meta-value">${safeNumber(job.OperationQuantity).toFixed(0)}</div></div>
                <div class="meta"><div class="label">M2M Complete</div><div class="meta-value">${safeNumber(job.OperationQtyReported).toFixed(0)}</div></div>
                <div class="meta"><div class="label">M2M Remaining</div><div class="meta-value">${safeNumber(job.OperationQtyRemaining).toFixed(0)}</div></div>
                <div class="meta"><div class="label">Type Code</div><div class="meta-value">${escapeHtml(job.ActiveTypeCode || '')}</div></div>
            </div>
            ${description ? `<div class="description" title="${description}"><strong>Operation:</strong> ${description}</div>` : ''}
            <div class="history-wrap">
                ${historyCard(job.HistoricalProduction || {}, 'Production')}
                ${historyCard(job.HistoricalSetup || {}, 'Setup')}
            </div>
        </section>`;
    }).join('');
}

loadDashboard(true);
refreshTimer = setInterval(() => loadDashboard(false), 30000);
</script>
</body>
</html>"""


COMPLETED_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Completed Job Review</title>
<style>
:root {
    --navy: #14273d;
    --navy-2: #1f3a56;
    --blue: #2767a8;
    --blue-soft: #eaf2fb;
    --ink: #1c2733;
    --muted: #667587;
    --line: #d9e1e8;
    --panel: #ffffff;
    --canvas: #eef2f6;
    --soft: #f7f9fb;
    --good-bg: #edf8f2;
    --good-line: #abd9c3;
    --good-text: #17633f;
    --bad-bg: #fff1f1;
    --bad-line: #e5b3b3;
    --bad-text: #9b2f2f;
    --high-bg: #eef7ff;
    --high-line: #b7d6ee;
    --high-text: #1e537f;
    --data-bg: #f3f0f7;
    --data-line: #d4c8e0;
    --data-text: #563c72;
    --shadow: 0 7px 20px rgba(22, 38, 55, .08);
}
* { box-sizing: border-box; }
body { margin: 0; font-family: "Segoe UI", Arial, sans-serif; background: var(--canvas); color: var(--ink); }
.topbar {
    background: linear-gradient(135deg, var(--navy), var(--navy-2)); color: white;
    min-height: 64px; display: flex; align-items: center; justify-content: space-between;
    gap: 18px; padding: 9px 24px; box-shadow: 0 3px 11px rgba(0,0,0,.14);
}
.brand { display: flex; align-items: center; gap: 11px; font-size: 19px; font-weight: 750; }
.brand-mark { width: 38px; height: 38px; border-radius: 9px; display: inline-flex; align-items: center; justify-content: center; background: white; color: var(--navy); font-size: 12px; font-weight: 850; }
.nav { display: flex; gap: 6px; flex-wrap: wrap; padding: 5px; background: rgba(255,255,255,.10); border: 1px solid rgba(255,255,255,.20); border-radius: 11px; }
.nav a { text-decoration: none; color: #e5edf6; padding: 9px 14px; border-radius: 8px; font-weight: 800; font-size: 13px; min-width: 160px; text-align: center; border: 1px solid transparent; }
.nav a:hover { background: rgba(255,255,255,.14); color: white; }
.nav a.active { background: white; color: var(--navy); border-color: white; box-shadow: 0 3px 10px rgba(0,0,0,.16); }
.nav a.active::after { content: "CURRENT VIEW"; display: block; margin-top: 2px; font-size: 8px; letter-spacing: .9px; color: var(--blue); }
.nav a:not(.active)::after { content: "SWITCH VIEW"; display: block; margin-top: 2px; font-size: 8px; letter-spacing: .9px; color: #b9cada; }
.shell { max-width: 1600px; margin: 0 auto; padding: 24px; }
.page-hero { margin-bottom: 18px; }
.eyebrow { text-transform: uppercase; letter-spacing: 1.3px; color: var(--blue); font-size: 12px; font-weight: 800; margin-bottom: 6px; }
h1 { margin: 0; font-size: 31px; letter-spacing: -.35px; }
.subtitle { color: var(--muted); margin-top: 7px; line-height: 1.5; max-width: 1050px; }
.toolbar, .summary, .jobs-card, .status-box {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); margin-bottom: 16px;
}
.toolbar { padding: 16px; display: flex; align-items: end; gap: 10px; flex-wrap: wrap; }
.field { display: flex; flex-direction: column; gap: 5px; }
.label { font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .5px; color: var(--muted); }
input, select, button { font: inherit; min-height: 39px; border: 1px solid #c8d2dc; border-radius: 8px; }
input, select { padding: 8px 10px; background: white; color: var(--ink); }
input:focus, select:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(39,103,168,.13); }
button { padding: 8px 13px; background: white; color: var(--ink); font-weight: 750; cursor: pointer; }
button:hover { background: #f3f6f9; }
.primary { background: var(--blue); color: white; border-color: var(--blue); }
.primary:hover { filter: brightness(.95); background: var(--blue); }
.quick { min-height: 34px; font-size: 12px; }
.spacer { flex: 1 1 20px; }
.summary { padding: 16px; }
.summary-title { font-size: 16px; font-weight: 850; color: var(--navy); margin-bottom: 11px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.summary-range { color: var(--muted); font-size: 12px; font-weight: 650; }
.summary-grid { display: grid; grid-template-columns: repeat(8, minmax(120px, 1fr)); gap: 9px; }
.summary-card { background: var(--soft); border: 1px solid var(--line); border-radius: 9px; padding: 11px; color: var(--muted); font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing: .35px; }
.summary-value { margin-top: 5px; font-size: 26px; font-weight: 400; color: var(--ink); text-transform: none; letter-spacing: 0; }
.summary-card.bad { background: var(--bad-bg); border-color: var(--bad-line); }
.summary-card.bad .summary-value { color: var(--bad-text); }
.summary-card.high { background: var(--high-bg); border-color: var(--high-line); }
.summary-card.high .summary-value { color: var(--high-text); }
.summary-card.data { background: var(--data-bg); border-color: var(--data-line); }
.summary-card.data .summary-value { color: var(--data-text); }
.summary-card.good { background: var(--good-bg); border-color: var(--good-line); }
.summary-card.good .summary-value { color: var(--good-text); }
.jobs-card { overflow: visible; }
.jobs-tools { padding: 13px 14px; display: flex; gap: 9px; align-items: end; flex-wrap: wrap; border-bottom: 1px solid var(--line); background: #fbfcfd; }
.jobs-tools .field input { min-width: 240px; }
.job-count { margin-left: auto; color: var(--muted); font-size: 12px; font-weight: 750; align-self: center; }
.table-wrap { width: 100%; overflow-x: auto; }
.jobs-table-wrap { overflow: visible; }
/* Fit Completed Job Review output to the viewport without shrinking the enlarged data font. */
.jobs-table { width: 100%; table-layout: fixed; }
.jobs-table th, .jobs-table td { white-space: normal; overflow-wrap: anywhere; }
.jobs-table td { font-size: 15px; font-weight: 400; padding: 8px 5px; }
.jobs-table .job-id, .jobs-table .part-id { font-weight: 400; }
.jobs-table .status-badge { font-weight: 500; }
.jobs-table thead th { position: sticky; top: 0; z-index: 40; background: #f5f8fb; box-shadow: 0 2px 6px rgba(20,39,61,.18); }
table { width: 100%; border-collapse: separate; border-spacing: 0; min-width: 1260px; }
th { background: #f5f8fb; color: #526171; text-align: left; padding: 9px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; font-size: 12px; text-transform: uppercase; letter-spacing: .4px; position: sticky; top: 0; z-index: 2; }
td { padding: 10px 8px; border-bottom: 1px solid #e8edf2; white-space: nowrap; font-size: 15px; font-weight: 400; vertical-align: middle; }
.job-row { cursor: pointer; }
.job-row:hover { filter: brightness(.985); }
.job-row.status-below { background: var(--bad-bg); }
.job-row.status-high { background: var(--high-bg); }
.job-row.status-data { background: var(--data-bg); }
.job-row.status-normal:nth-of-type(even) { background: #fbfcfd; }
.expand-cell { width: 34px; text-align: center; font-size: 20px; color: var(--blue); font-weight: 850; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.job-id { font-weight: 850; color: var(--navy); }
.part-id { font-weight: 700; }
.status-badge { display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 11px; font-weight: 850; letter-spacing: .2px; }
.badge-below { background: #f4c7c7; color: #842626; }
.badge-normal { background: #e4eaf0; color: #4e5d6c; }
.badge-high { background: #cfe5f8; color: #1e537f; }
.badge-data { background: #ddd4ea; color: #563c72; }
.detail-row { display: none; }
.detail-row.open { display: table-row; }
.detail-row > td { padding: 0; background: #f7f9fb; }
.detail-panel { padding: 14px; border-bottom: 2px solid #c8d5e2; }
.detail-title { font-weight: 850; color: var(--navy); margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.detail-note { color: var(--muted); font-size: 11px; font-weight: 600; }

/* Match the Part Labor History operation-card presentation. */
.review-operation {
    background: var(--panel); margin-bottom: 14px; border: 1px solid var(--line);
    border-radius: 12px; overflow: hidden; box-shadow: var(--shadow);
}
.review-operation:last-child { margin-bottom: 0; }
.review-operation-header {
    background: linear-gradient(135deg, var(--navy), var(--navy-2)); color: white;
    padding: 16px 18px; display: flex; justify-content: space-between; align-items: center; gap: 16px;
}
.review-operation-heading { min-width: 0; }
.review-operation-number { font-size: 22px; font-weight: 850; display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.review-operation-workcenter { font-size: 16px; font-weight: 700; color: #c8d8e8; }
.review-operation-completed { margin-top: 5px; line-height: 1.4; color: #d9e5ef; font-size: 12px; }

.review-operation-summary {
    display: grid; grid-template-columns: repeat(5, minmax(125px, 1fr)); gap: 10px;
    padding: 14px; background: #f5f8fb; border-bottom: 1px solid var(--line); overflow-x: auto;
}
.review-summary-card {
    background: white; border: 1px solid var(--line); border-radius: 9px; padding: 11px;
    color: var(--muted); font-size: 11px; font-weight: 750; text-transform: uppercase; letter-spacing: .35px;
}
.review-summary-value { font-size: 23px; font-weight: 400; color: var(--ink); margin-top: 5px; text-transform: none; letter-spacing: 0; }
.review-summary-card.bad { background: var(--bad-bg); border-color: var(--bad-line); }
.review-summary-card.bad .review-summary-value { color: var(--bad-text); }
.review-summary-card.high { background: var(--high-bg); border-color: var(--high-line); }
.review-summary-card.high .review-summary-value { color: var(--high-text); }
.review-summary-card.data { background: var(--data-bg); border-color: var(--data-line); }
.review-summary-card.data .review-summary-value { color: var(--data-text); }
.review-summary-card.good { background: var(--good-bg); border-color: var(--good-line); }
.review-summary-card.good .review-summary-value { color: var(--good-text); }
.review-summary-card.ignored { background: #f2f4f6; border-color: #d6dce2; }
.review-summary-card.ignored .review-summary-value { color: #65717c; }
.review-labor-title {
    padding: 11px 15px; background: var(--blue-soft); color: var(--navy); font-size: 16px;
    font-weight: 900; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
}
.review-labor-table { width: 100%; min-width: 0; table-layout: fixed; background: white; }
.review-labor-table th { position: static; background: #f7f9fb; font-size: 14px; font-weight: 700; padding: 10px 6px; white-space: normal; }
.review-labor-table td { font-size: 16px; font-weight: 400; padding: 10px 6px; white-space: normal; overflow-wrap: anywhere; }
.review-labor-table td.num { font-weight: 400; }
.review-labor-table .status-badge, .review-labor-table .ignored-badge { font-size: 12px; font-weight: 500; }
.ignored-strip { padding: 9px 14px; background: #f2f4f6; color: #65717c; font-size: 11px; font-weight: 700; border-top: 1px solid var(--line); }
.ignored-badge { display:inline-block; padding:4px 8px; border-radius:999px; background:#e4e7ea; color:#56616b; font-size:10px; font-weight:800; letter-spacing:.35px; }
.variance-good { color: var(--good-text); font-weight: 400; }
.variance-bad { color: var(--bad-text); font-weight: 400; }
.eff-bad { color: var(--bad-text); font-weight: 400; }
.eff-high { color: var(--high-text); font-weight: 400; }
.eff-normal { color: var(--good-text); font-weight: 400; }
.status-box { padding: 14px 16px; color: var(--navy); border-left: 4px solid var(--blue); font-weight: 750; }
.status-box.error { color: #a32f2f; border-left-color: #c74646; }
.footer { color: #7a8794; text-align: center; font-size: 11px; padding: 10px 0 24px; }
@media (max-width: 1150px) { .summary-grid { grid-template-columns: repeat(4, 1fr); } }
@media (max-width: 800px) {
    .jobs-table-wrap { overflow-x: auto; }
    .topbar { align-items: stretch; flex-direction: column; padding: 11px 14px; }
    .nav { width: 100%; }
    .nav a { flex: 1 1 145px; min-width: 0; font-size: 11px; padding: 8px 5px; }
    .shell { padding: 12px; }
    .summary-grid { grid-template-columns: repeat(2, 1fr); }
    .toolbar .field, .toolbar input, .toolbar select, .toolbar button { width: 100%; }
    .jobs-tools .field, .jobs-tools input, .jobs-tools select, .jobs-tools button { width: 100%; }
    .job-count { margin-left: 0; }
}
</style>
</head>
<body>
<header class="topbar">
    <div class="brand"><span class="brand-mark">M2M</span><span>M2M Labor Analytics</span></div>
    <nav class="nav">
        <a href="/">Part Labor History</a>
        <a href="/live">Live Employee Performance</a>
        <a href="/dashboard">Live Job Dashboard</a>
        <a class="active" href="/completed">Completed Job Review</a>
    </nav>
</header>
<main class="shell">
    <section class="page-hero">
        <div class="eyebrow">Manufacturing Intelligence</div>
        <h1>Completed Job Review</h1>
        <div class="subtitle">Review jobs by their final routing completion date. The selected date range chooses the completed jobs; efficiency uses all historical Setup and Production labor charged to each selected job.</div>
    </section>

    <div class="toolbar">
        <div class="field"><label class="label" for="startDate">From Completion Date</label><input id="startDate" type="date"></div>
        <div class="field"><label class="label" for="endDate">To Completion Date</label><input id="endDate" type="date"></div>
        <button class="primary" onclick="loadReview()">Load Review</button>
        <button class="quick" onclick="setPreviousWeek(true)">Previous Week</button>
        <button class="quick" onclick="setThisWeek(true)">This Week</button>
        <button class="quick" onclick="setLastDays(28, true)">Last 4 Weeks</button>
        <button class="quick" onclick="setLastDays(30, true)">Last 30 Days</button>
    </div>

    <div id="statusArea" class="status-box">Loading previous week...</div>
    <div id="summaryArea"></div>

    <section id="jobsCard" class="jobs-card" style="display:none;">
        <div class="jobs-tools">
            <div class="field"><label class="label" for="jobSearch">Job / Part Search</label><input id="jobSearch" type="text" placeholder="Filter job or part" oninput="renderJobs()"></div>
            <div class="field"><label class="label" for="statusFilter">Review Status</label>
                <select id="statusFilter" onchange="renderJobs()">
                    <option value="">All Statuses</option>
                    <option value="BELOW 80%">Below 80%</option>
                    <option value="NORMAL">Normal 80–124.9%</option>
                    <option value="HIGH PERF REVIEW">High Perf Review ≥125%</option>
                    <option value="DATA REVIEW">Data Review</option>
                </select>
            </div>
            <button onclick="expandAll()">Expand All</button>
            <button onclick="collapseAll()">Collapse All</button>
            <div id="jobCount" class="job-count"></div>
        </div>
        <div class="table-wrap jobs-table-wrap">
            <table class="jobs-table">
                <thead>
                    <tr>
                        <th></th><th>Job</th><th>Part</th><th>Rev</th><th>Completion</th><th>Ops</th>
                        <th class="num">Actual Hrs</th><th class="num">Estimated Hrs</th><th class="num">Variance<br>(Est - Actual)</th>
                        <th class="num">Efficiency</th><th>Status</th>
                    </tr>
                </thead>
                <tbody id="jobsBody"></tbody>
            </table>
        </div>
    </section>

    <div class="footer">Completion date = latest completed routing operation • Setup estimate counted once only when setup labor exists • Production estimate based on reported completed quantity • Internal use</div>
</main>
<script>
let reviewData = {jobs: [], operations: [], summary: {}};
let operationsByJob = {};
let openJobs = new Set();

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}
function safeNumber(value) { const n = Number(value); return Number.isFinite(n) ? n : 0; }
function formatInputDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
}
function displayDate(value) {
    if (!value) return '—';
    const raw = String(value).substring(0, 10);
    const parts = raw.split('-');
    if (parts.length !== 3) return escapeHtml(raw);
    return `${Number(parts[1])}/${Number(parts[2])}/${parts[0]}`;
}
function moneylessNumber(value) { return safeNumber(value).toFixed(2); }
function efficiencyText(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(1) + '%' : '—';
}
function rowClass(status) {
    if (status === 'BELOW 80%') return 'status-below';
    if (status === 'HIGH PERF REVIEW') return 'status-high';
    if (status === 'DATA REVIEW') return 'status-data';
    return 'status-normal';
}
function opClass(status) {
    if (status === 'BELOW 80%') return 'op-below';
    if (status === 'HIGH PERF REVIEW') return 'op-high';
    if (status === 'DATA REVIEW') return 'op-data';
    if (status === 'IGNORED') return 'op-ignored';
    return '';
}
function badge(status) {
    const css = status === 'BELOW 80%' ? 'badge-below'
        : status === 'HIGH PERF REVIEW' ? 'badge-high'
        : status === 'DATA REVIEW' ? 'badge-data' : 'badge-normal';
    return `<span class="status-badge ${css}">${escapeHtml(status || '')}</span>`;
}
function efficiencyCss(status) {
    if (status === 'BELOW 80%') return 'eff-bad';
    if (status === 'HIGH PERF REVIEW') return 'eff-high';
    if (status === 'NORMAL') return 'eff-normal';
    return '';
}
function varianceCss(value) {
    const n = safeNumber(value);
    return n >= 0 ? 'variance-good' : 'variance-bad';
}
function mondayOfWeek(d) {
    const copy = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const day = copy.getDay();
    const diff = day === 0 ? -6 : 1 - day;
    copy.setDate(copy.getDate() + diff);
    return copy;
}
function setPreviousWeek(loadNow) {
    const today = new Date();
    const thisMonday = mondayOfWeek(today);
    const end = new Date(thisMonday); end.setDate(end.getDate() - 1);
    const start = new Date(end); start.setDate(start.getDate() - 6);
    document.getElementById('startDate').value = formatInputDate(start);
    document.getElementById('endDate').value = formatInputDate(end);
    if (loadNow) loadReview();
}
function setThisWeek(loadNow) {
    const today = new Date();
    document.getElementById('startDate').value = formatInputDate(mondayOfWeek(today));
    document.getElementById('endDate').value = formatInputDate(today);
    if (loadNow) loadReview();
}
function setLastDays(days, loadNow) {
    const end = new Date();
    const start = new Date(end);
    start.setDate(start.getDate() - (days - 1));
    document.getElementById('startDate').value = formatInputDate(start);
    document.getElementById('endDate').value = formatInputDate(end);
    if (loadNow) loadReview();
}

async function loadReview() {
    const start = document.getElementById('startDate').value;
    const end = document.getElementById('endDate').value;
    const statusArea = document.getElementById('statusArea');
    const summaryArea = document.getElementById('summaryArea');
    const jobsCard = document.getElementById('jobsCard');

    if (!start || !end) {
        statusArea.className = 'status-box error';
        statusArea.textContent = 'Select both completion dates.';
        return;
    }
    if (start > end) {
        statusArea.className = 'status-box error';
        statusArea.textContent = 'From date cannot be after To date.';
        return;
    }

    statusArea.className = 'status-box';
    statusArea.textContent = 'Loading completed jobs and all historical labor...';
    summaryArea.innerHTML = '';
    jobsCard.style.display = 'none';
    openJobs.clear();

    try {
        const url = `/api/completed-job-review?start_date=${encodeURIComponent(start)}&end_date=${encodeURIComponent(end)}`;
        const response = await fetch(url);
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'Could not load Completed Job Review.');

        reviewData = payload;
        operationsByJob = {};
        (payload.operations || []).forEach(op => {
            const job = String(op.JobNumber || '').trim();
            if (!operationsByJob[job]) operationsByJob[job] = [];
            operationsByJob[job].push(op);
        });

        renderSummary();
        renderJobs();
        jobsCard.style.display = 'block';
        statusArea.textContent = payload.jobs.length
            ? `Loaded ${payload.jobs.length} completed jobs.`
            : 'No completed jobs were found for this date range.';
    } catch (error) {
        statusArea.className = 'status-box error';
        statusArea.textContent = error.message;
    }
}

function renderSummary() {
    const s = reviewData.summary || {};
    const efficiency = Number(s.OverallEfficiencyPercent);
    const effClass = Number.isFinite(efficiency) && efficiency >= 80 ? 'good' : 'bad';
    document.getElementById('summaryArea').innerHTML = `
        <section class="summary">
            <div class="summary-title">
                <span>Completed Job Summary</span>
                <span class="summary-range">${displayDate(reviewData.start_date)} through ${displayDate(reviewData.end_date)}</span>
            </div>
            <div class="summary-grid">
                <div class="summary-card">Total Jobs<div class="summary-value">${safeNumber(s.TotalJobs).toFixed(0)}</div></div>
                <div class="summary-card bad">Below 80%<div class="summary-value">${safeNumber(s.Below80Jobs).toFixed(0)}</div></div>
                <div class="summary-card">Normal<div class="summary-value">${safeNumber(s.NormalJobs).toFixed(0)}</div></div>
                <div class="summary-card high">High Perf Review<div class="summary-value">${safeNumber(s.HighPerfReviewJobs).toFixed(0)}</div></div>
                <div class="summary-card data">Data Review<div class="summary-value">${safeNumber(s.DataReviewJobs).toFixed(0)}</div></div>
                <div class="summary-card">Actual Hrs<div class="summary-value">${moneylessNumber(s.TotalActualHours)}</div></div>
                <div class="summary-card">Estimated Hrs<div class="summary-value">${moneylessNumber(s.TotalEstimatedHours)}</div></div>
                <div class="summary-card ${effClass}">Overall Efficiency<div class="summary-value">${efficiencyText(s.OverallEfficiencyPercent)}</div></div>
            </div>
        </section>`;
}

function renderJobs() {
    const body = document.getElementById('jobsBody');
    if (!body) return;
    const search = (document.getElementById('jobSearch')?.value || '').trim().toLowerCase();
    const status = document.getElementById('statusFilter')?.value || '';
    const jobs = (reviewData.jobs || []).filter(job => {
        const haystack = `${job.JobNumber || ''} ${job.PartNumber || ''} ${job.PartRevision || ''}`.toLowerCase();
        return (!search || haystack.includes(search)) && (!status || job.ReviewStatus === status);
    });

    document.getElementById('jobCount').textContent = `Showing ${jobs.length} of ${(reviewData.jobs || []).length} jobs`;

    if (!jobs.length) {
        body.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#667587;padding:18px;">No jobs match the current filters.</td></tr>';
        return;
    }

    body.innerHTML = jobs.map(job => {
        const jobNo = String(job.JobNumber || '').trim();
        const safeId = jobNo.replace(/[^A-Za-z0-9_-]/g, '_');
        const isOpen = openJobs.has(jobNo);
        return `
            <tr class="job-row ${rowClass(job.ReviewStatus)}" data-job="${escapeHtml(jobNo)}" data-safe="${safeId}" onclick="toggleJob(this)">
                <td class="expand-cell" id="icon-${safeId}">${isOpen ? '−' : '+'}</td>
                <td class="job-id">${escapeHtml(jobNo)}</td>
                <td class="part-id">${escapeHtml(job.PartNumber || '')}</td>
                <td>${escapeHtml(job.PartRevision || '')}</td>
                <td>${displayDate(job.CompletionDate)}</td>
                <td class="num">${safeNumber(job.OperationCount).toFixed(0)}</td>
                <td class="num">${moneylessNumber(job.ActualHours)}</td>
                <td class="num">${moneylessNumber(job.EstimatedHours)}</td>
                <td class="num ${varianceCss(job.VarianceHours)}">${moneylessNumber(job.VarianceHours)}</td>
                <td class="num ${efficiencyCss(job.ReviewStatus)}">${efficiencyText(job.EfficiencyPercent)}</td>
                <td>${badge(job.ReviewStatus)}</td>
            </tr>
            <tr id="detail-${safeId}" class="detail-row ${isOpen ? 'open' : ''}">
                <td colspan="11">${renderOperationDetail(jobNo)}</td>
            </tr>`;
    }).join('');
}

function renderOperationDetail(jobNo) {
    const ops = operationsByJob[jobNo] || [];
    if (!ops.length) {
        return `<div class="detail-panel"><div class="detail-title">Operation Detail</div><div class="detail-note">No valid Setup or Production labor detail was found for this completed job.</div></div>`;
    }

    const operationCards = ops.map(op => {
        const ignored = Number(op.IgnoreVariance || 0) === 1 || op.ReviewStatus === 'IGNORED';
        const actual = safeNumber(op.ActualHours);
        const estimated = safeNumber(op.EstimatedHours);
        const difference = actual - estimated; // positive = over estimate
        const differenceText = ignored ? '—' : ((difference >= 0 ? '+' : '') + difference.toFixed(2));
        const differenceClass = ignored ? 'ignored' : (difference > 0 ? 'bad' : 'good');
        const efficiency = ignored ? '—' : efficiencyText(op.EfficiencyPercent);
        const status = ignored ? '<span class="ignored-badge">IGNORED</span>' : badge(op.ReviewStatus);
        const statusClass = ignored ? 'ignored' : op.ReviewStatus === 'BELOW 80%' ? 'bad' : op.ReviewStatus === 'HIGH PERF REVIEW' ? 'high' : op.ReviewStatus === 'DATA REVIEW' ? 'data' : 'good';
        const wcId = escapeHtml(op.WorkCenter || '');
        const wcName = escapeHtml(op.WorkCenterName || '');
        const wcText = wcName && wcId ? `WC ${wcId} • ${wcName}` : (wcName || (wcId ? `WC ${wcId}` : ''));

        const setupActual = safeNumber(op.SetupActualHours);
        const setupEstimated = safeNumber(op.SetupEstimatedHours);
        const prodActual = safeNumber(op.ProductionActualHours);
        const prodEstimated = safeNumber(op.ProductionEstimatedHours);
        const prodQty = safeNumber(op.ProductionQty);

        const laborSection = (title, est, act, qty, includeQty) => {
            const diff = act - est;
            const valid = est >= 0.01 && act >= 0.01;
            const eff = valid ? (est / act * 100) : null;
            const laborStatus = ignored ? 'IGNORED'
                : !valid ? 'DATA REVIEW'
                : eff < 80 ? 'BELOW 80%'
                : eff >= 125 ? 'HIGH PERF REVIEW'
                : 'NORMAL';
            const laborStatusHtml = ignored ? '<span class="ignored-badge">IGNORED</span>' : badge(laborStatus);
            const diffCss = ignored ? '' : (diff > 0 ? 'variance-bad' : diff < 0 ? 'variance-good' : '');
            return `
                <div class="review-labor-title">${title}</div>
                <div class="table-wrap">
                    <table class="review-labor-table">
                        <thead><tr>
                            ${includeQty ? '<th class="num">Qty Complete</th>' : ''}
                            <th class="num">Estimated Hrs</th><th class="num">Actual Hrs</th>
                            <th class="num">Variance<br>(Actual - Est)</th><th class="num">Efficiency</th><th>Status</th>
                        </tr></thead>
                        <tbody><tr>
                            ${includeQty ? `<td class="num">${qty.toFixed(2)}</td>` : ''}
                            <td class="num">${est.toFixed(2)}</td>
                            <td class="num">${act.toFixed(2)}</td>
                            <td class="num ${diffCss}">${ignored ? '—' : ((diff >= 0 ? '+' : '') + diff.toFixed(2))}</td>
                            <td class="num ${ignored ? '' : efficiencyCss(laborStatus)}">${ignored || eff === null ? '—' : eff.toFixed(1) + '%'}</td>
                            <td>${laborStatusHtml}</td>
                        </tr></tbody>
                    </table>
                </div>`;
        };

        return `
            <section class="review-operation">
                <div class="review-operation-header">
                    <div class="review-operation-heading">
                        <div class="review-operation-number">Operation ${escapeHtml(op.OperationNumber)}${wcText ? `<span class="review-operation-workcenter">• ${wcText}</span>` : ''}</div>
                        <div class="review-operation-completed">Completed ${displayDate(op.OperationCompletionDate)}</div>
                    </div>
                </div>
                <div class="review-operation-summary">
                    <div class="review-summary-card">Estimated Hrs<div class="review-summary-value">${estimated.toFixed(2)}</div></div>
                    <div class="review-summary-card">Actual Hrs<div class="review-summary-value">${actual.toFixed(2)}</div></div>
                    <div class="review-summary-card ${differenceClass}">Variance<div class="review-summary-value">${differenceText}</div></div>
                    <div class="review-summary-card ${statusClass}">Efficiency<div class="review-summary-value">${efficiency}</div></div>
                    <div class="review-summary-card ${statusClass}">Status<div class="review-summary-value" style="font-size:13px">${status}</div></div>
                </div>
                ${laborSection('Setup', setupEstimated, setupActual, 0, false)}
                ${laborSection('Production', prodEstimated, prodActual, prodQty, true)}
                ${ignored ? '<div class="ignored-strip">Quality Inspection / Shipping operation — excluded from job variance and efficiency totals.</div>' : ''}
            </section>`;
    }).join('');

    return `
        <div class="detail-panel">
            <div class="detail-title">
                <span>Estimated vs. Actual Time by Operation</span>
                <span class="detail-note">Formatted to match Part Labor History. Quality Inspection and Shipping remain visible but are excluded from review totals.</span>
            </div>
            ${operationCards}
        </div>`;
}

function toggleJob(row) {
    const safeId = row.dataset.safe || '';
    const jobNo = row.dataset.job || '';
    const detail = document.getElementById('detail-' + safeId);
    const icon = document.getElementById('icon-' + safeId);
    if (!detail || !icon) return;
    const opening = !detail.classList.contains('open');
    detail.classList.toggle('open', opening);
    icon.textContent = opening ? '−' : '+';
    if (opening) openJobs.add(jobNo); else openJobs.delete(jobNo);
}
function expandAll() {
    const visibleRows = document.querySelectorAll('#jobsBody .job-row');
    visibleRows.forEach(row => {
        const detail = row.nextElementSibling;
        if (detail && detail.classList.contains('detail-row')) {
            detail.classList.add('open');
            const icon = row.querySelector('.expand-cell');
            if (icon) icon.textContent = '−';
            const jobCell = row.querySelector('.job-id');
            if (jobCell) openJobs.add(jobCell.textContent.trim());
        }
    });
}
function collapseAll() {
    document.querySelectorAll('#jobsBody .detail-row').forEach(row => row.classList.remove('open'));
    document.querySelectorAll('#jobsBody .expand-cell').forEach(cell => cell.textContent = '+');
    openJobs.clear();
}

setPreviousWeek(false);
loadReview();
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HISTORY_HTML


@app.get("/live", response_class=HTMLResponse)
def live_page():
    return LIVE_HTML


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return DASHBOARD_HTML


@app.get("/completed", response_class=HTMLResponse)
def completed_page():
    return COMPLETED_HTML

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
