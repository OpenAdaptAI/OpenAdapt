-- assets/fixtures.sql

-- Insert sample recordings
INSERT INTO recording (timestamp, monitor_width, monitor_height, double_click_interval_seconds, double_click_distance_pixels, platform, task_description)
VALUES 
    ('2023-06-28 10:15:00', 1920, 1080, 0.5, 10, 'Windows', 'Task 1'),
    ('2023-06-29 14:30:00', 1366, 768, 0.3, 8, 'Mac', 'Task 2'),
    ('2023-06-30 09:45:00', 2560, 1440, 0.7, 12, 'Linux', 'Task 3');
