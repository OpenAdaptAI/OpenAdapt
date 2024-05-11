export type ActionEvent = {
    id: number | string;
    name?: string;
    timestamp: number;
    recording_timestamp: number;
    screenshot_timestamp?: number;
    window_event_timestamp: number;
    mouse_x: number | null;
    mouse_y: number | null;
    mouse_dx: number | null;
    mouse_dy: number | null;
    mouse_button_name: string | null;
    mouse_pressed: boolean | null;
    key_name: string | null;
    key_char: string | null;
    key_vk: number | null;
    canonical_key_name: string | null;
    canonical_key_char: string | null;
    canonical_key_vk: number | null;
    text?: string;
    canonical_text?: string;
    parent_id?: number;
    element_state: Record<string, any>;
    screenshot: string | null;
    diff: string | null;
    mask: string | null;
    dimensions?: { width: number, height: number };
    children?: ActionEvent[];
}
