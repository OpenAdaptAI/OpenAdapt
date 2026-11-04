SELECT x.id, x.user, x.patient_id, x.ev, x.tbl FROM (
  SELECT CONCAT('a', a.id) AS id, u.username AS user, p.pid AS patient_id,
    CASE a.method WHEN 'POST' THEN 'insert' WHEN 'DELETE' THEN 'delete' ELSE 'update' END AS ev,
    CASE a.request
      WHEN 'patient' THEN 'patient_data' WHEN 'allergy' THEN 'lists'
      WHEN 'medical_problem' THEN 'lists' WHEN 'medication' THEN 'lists'
      WHEN 'surgery' THEN 'lists' WHEN 'dental_issue' THEN 'lists'
      WHEN 'insurance' THEN 'insurance_data' WHEN 'appointment' THEN 'openemr_postcalendar_events'
      WHEN 'encounter' THEN 'form_encounter' WHEN 'message' THEN 'pnotes'
      WHEN 'immunization' THEN 'immunizations' WHEN 'prescription' THEN 'prescriptions'
      ELSE a.request END AS tbl
  FROM openemr.api_log a
  JOIN openemr.users u ON u.id = a.user_id
  JOIN openemr.patient_data p ON LOWER(INSERT(INSERT(INSERT(INSERT(HEX(p.uuid),9,0,'-'),14,0,'-'),19,0,'-'),24,0,'-')) =
    COALESCE(NULLIF(REGEXP_SUBSTR(a.request_url, '(?<=/patient/)[0-9a-f-]{36}'), ''), JSON_VALUE(a.response, '$.data.uuid'))
  WHERE a.method IN ('POST', 'PUT', 'PATCH', 'DELETE')
  UNION ALL
  SELECT CONCAT('l', l.id), l.user, l.patient_id, SUBSTRING_INDEX(l.event, '-', -1),
    -- The table the statement itself writes, read from its start only: text
    -- later in the statement (a value the agent typed) can't name a table.
    REGEXP_SUBSTR(CONVERT(FROM_BASE64(l.comments) USING utf8mb4), '(?i)^\\s*(?:insert\\s+(?:ignore\\s+)?into|replace\\s+into|update|delete\\s+from)\\s+`?\\K[a-z_]+')
  FROM openemr.log l
  -- Run 2: entries that name no patient too (a user's own settings), so the
  -- frame can attribute them by owner; REPLACE INTO is parsed like INSERT.
  WHERE l.user <> '' AND l.event REGEXP '-(insert|update|delete)$'
) x WHERE x.tbl IS NOT NULL AND x.tbl <> ''
  -- OpenEMR logs a UI write to patient_data under the session's previous
  -- chart (log.patient_id lags the record written), so those entries name
  -- the wrong record; API writes to patient_data come from api_log.
  AND NOT (x.id LIKE 'l%' AND x.tbl = 'patient_data')
