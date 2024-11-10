export enum UploadStatus {
    NOT_UPLOADED = 'not_uploaded',
    UPLOADING = 'uploading',
    UPLOADED = 'uploaded',
}

export type Recording = {
    id: number
    timestamp: number
    monitor_width: number
    monitor_height: number
    double_click_interval_seconds: number
    double_click_distance_pixels: number
    platform: string
    task_description: string
    video_start_time: number | null
    original_recording_id: number | null
    uploaded_key: string | null
    uploaded_to_custom_bucket: boolean
    upload_status: UploadStatus
}

export enum RecordingStatus {
    STOPPED = 'STOPPED',
    RECORDING = 'RECORDING',
    UNKNOWN = 'UNKNOWN',
}

export type ScrubbedRecording = {
    id: number
    recording_id: number
    recording: Pick<Recording, 'task_description'>
    provider: string
    original_recording: Pick<Recording, 'id' | 'task_description'>
    scrubbed: boolean
}
