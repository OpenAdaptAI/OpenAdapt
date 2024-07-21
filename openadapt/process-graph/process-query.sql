SELECT
    R.ID AS "case_id",
    'Title: ' || WE.TITLE || ', Action: ' || AE.NAME || ', Action_Target: ' || 
    CASE 
        WHEN AE.NAME = 'scroll' THEN '(' || AE.mouse_dx || ',' || AE.mouse_dy || ')'
        WHEN AE.NAME = 'move' THEN '(' || AE.mouse_x || ',' || AE.mouse_y || ')'
        WHEN AE.NAME = 'click' THEN AE.mouse_button_name
        WHEN AE.NAME IN ('press', 'release') THEN AE.CANONICAL_KEY_NAME
        ELSE 'N/A'
    END AS "activity",
    DATETIME(AE."timestamp", 'unixepoch', 'localtime') AS "timestamp",
    COALESCE(AE."timestamp" - LAG(AE."timestamp") OVER (ORDER BY AE."timestamp"), 0) AS "costs",
    AE.NAME AS "resource"
FROM
    RECORDING R
    INNER JOIN ACTION_EVENT AE ON R."timestamp" = AE.RECORDING_TIMESTAMP
    INNER JOIN WINDOW_EVENT WE ON R."timestamp" = WE.RECORDING_TIMESTAMP AND WE."timestamp" = AE.WINDOW_EVENT_TIMESTAMP
WHERE
    R.ID = 1
ORDER BY
    R.ID,
    AE."timestamp";
