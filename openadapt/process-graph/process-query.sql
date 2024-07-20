select r.id as case_id, 
	we.title as activity, 
    -- ae."timestamp" as timestamp,
    datetime(ae."timestamp", 'unixepoch', 'localtime') AS "timestamp",
	COALESCE(ae."timestamp" - LAG(ae."timestamp") OVER (ORDER BY ae."timestamp"), 0) as costs,
	ae.name	as resource
from recording r
inner join action_event ae on r."timestamp" = ae.recording_timestamp 
inner join window_event we on r."timestamp" = we.recording_timestamp and we."timestamp" = ae.window_event_timestamp
where r.id = 1
order by r.id, ae."timestamp";
