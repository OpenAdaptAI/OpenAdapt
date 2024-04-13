export type ActionEvent = {
    id: number | string;
    name?: string;
    timestamp: number;
    recording_timestamp: number;
    screenshot_timestamp?: number;
    window_event_timestamp: number;
    mouse_x?: number;
    mouse_y?: number;
    mouse_dx: number | null;
    mouse_dy: number | null;
    mouse_button_name?: string;
    mouse_pressed?: boolean;
    key_name?: string;
    key_char: string | null;
    key_vk: number | null;
    canonical_key_name: string | null;
    canonical_key_char: string | null;
    canonical_key_vk?: number;
    text?: string;
    canonical_text?: string;
    parent_id?: number;
    element_state: string | null;
    screenshot: string | null;
    children?: ActionEvent[];
}
