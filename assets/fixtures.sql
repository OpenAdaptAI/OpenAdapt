-- assets/fixtures.sql

-- Insert sample recordings
INSERT INTO recording (timestamp, monitor_width, monitor_height, double_click_interval_seconds, double_click_distance_pixels, platform, task_description)
VALUES
    (1689889605.9053426, 1920, 1080, 0.5, 4, 'win32', 'type l');

-- Insert sample action_events
INSERT INTO action_event (name, timestamp, recording_timestamp, screenshot_timestamp, window_event_timestamp, mouse_x, mouse_y, mouse_dx, mouse_dy, mouse_button_name, mouse_pressed, key_name, key_char, key_vk, canonical_key_name, canonical_key_char, canonical_key_vk, parent_id, element_state)
VALUES
    ('press', 1690049582.7713714, 1689889605.9053426, 1690049582.7686925, 1690049556.2166219, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'l', '76', NULL, 'l', NULL, NULL, 'null'),
    ('release', 1690049582.826782, 1689889605.9053426, 1690049582.7686925, 1690049556.2166219, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'l', '76', NULL, 'l', NULL, NULL, 'null');

-- Insert sample screenshots
INSERT INTO screenshot (recording_timestamp, timestamp, png_data)
VALUES
    (1689889605.9053426, 1690042711.774856, x'89504E470D0A1A0A0000000D49484452000000010000000108060000009077BF8A0000000A4944415408D7636000000005000000008D2B4233000000000049454E44AE426082');
    -- PNG data represents a 1x1 pixel image with a white pixel

-- Insert sample window_events
INSERT INTO window_event (recording_timestamp, timestamp, state, title, left, top, width, height, window_id)
VALUES
    (1689889605.9053426, 1690042703.7413292, '{"title": "recording.txt - openadapt - Visual Studio Code", "left": -9, "top": -9, "width": 1938, "height": 1048, "meta": {"class_name": "Chrome_WidgetWin_1", "control_id": 0, "rectangle": {"left": 0, "top": 0, "right": 1920, "bottom": 1030}, "is_visible": true, "is_enabled": true, "control_count": 0}}', 'recording.txt - openadapt - Visual Studio Code', -9, -9, 1938, 1048, '0');

-- Insert sample performance_stats
INSERT INTO performance_stat (recording_timestamp, event_type, start_time, end_time, window_id)
VALUES
    (1689889605.9053426, 'action', 1690042703, 1690042711, 1),
    (1689889605.9053426, 'action', 1690042712, 1690042720, 1);
    -- Add more rows as needed for additional performance_stats

-- Insert sample memory_stats
INSERT INTO memory_stat (recording_timestamp, memory_usage_bytes, timestamp)
VALUES
    (1689889605.9053426, 524288, 1690042703),
    (1689889605.9053426, 1048576, 1690042711);
    -- Add more rows as needed for additional memory_stats
