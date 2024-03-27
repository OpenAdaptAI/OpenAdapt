export type Recording = {
    id: number;
    timestamp: number;
    monitor_width: number;
    monitor_height: number;
    double_click_interval_seconds: number;
    double_click_distance_pixels: number;
    platform: string;
    task_description: string;
    video_start_time: number | null;
}

export enum RecordingStatus {
    STOPPED = 'STOPPED',
    RECORDING = 'RECORDING',
    UNKNOWN = 'UNKNOWN',
}
