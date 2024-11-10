import { Recording } from './recording'

export enum ScrubbingStatus {
    STOPPED = 'STOPPED',
    SCRUBBING = 'SCRUBBING',
    UNKNOWN = 'UNKNOWN',
}

export type ScrubbingUpdate = {
    num_action_events_scrubbed: number
    num_screenshots_scrubbed: number
    num_window_events_scrubbed: number
    total_action_events: number
    total_screenshots: number
    total_window_events: number
    recording: Pick<Recording, 'id' | 'task_description'>
    provider: string
    copying_recording?: boolean
    error?: string
}
