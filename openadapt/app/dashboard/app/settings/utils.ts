export function validateAPIKeysSettings(settings: Record<string, string>) {
    const errors: Record<string, string> = {}
    if (settings.OPENADAPT_API_KEY.length > 0) {
        return errors;
    }
    if (settings.AWS_SEGMENT_API_KEY.length === 0 && settings.REPLICATE_API_KEY.length === 0) {
        errors.REPLICATE_API_KEY = errors.AWS_SEGMENT_API_KEY = 'One of AWS_SEGMENT_API_KEY or REPLICATE_API_KEY is required'
    }
    if (settings.OPENAI_API_KEY.length === 0 && settings.ANTHROPIC_API_KEY.length === 0 && settings.GOOGLE_API_KEY.length === 0) {
        errors.OPENAI_API_KEY = errors.ANTHROPIC_API_KEY = errors.GOOGLE_API_KEY = 'One of OPENAI_API_KEY, ANTHROPIC_API_KEY or GOOGLE_API_KEY is required'
    }

    if (settings.OPENADAPT_API_KEY.length === 0 && Object.keys(errors).length > 0) {
        errors.OPENADAPT_API_KEY = 'Either OPENADAPT_API_KEY or a combination of other keys is required'
    }
    return errors
}


export function validateScrubbingSettings(settings: Record<string, string>) {
    const errors: Record<string, string> = {}
    if (settings.SCRUB_ENABLED) {
        return errors;
    }
    if (settings.SCRUB_CHAR.length === 0) {
        errors.SCRUB_CHAR = 'Scrubbing character is required'
    }
    if (settings.SCRUB_CHAR.length > 1) {
        errors.SCRUB_CHAR = 'Scrubbing character must be a single character'
    }
    if (settings.SCRUB_LANGUAGE.length === 0) {
        errors.SCRUB_LANGUAGE = 'Scrubbing language is required'
    }
    if (settings.SCRUB_LANGUAGE.length > 2) {
        errors.SCRUB_LANGUAGE = 'Scrubbing language must be a two character language code'
    }
    if (settings.SCRUB_FILL_COLOR.length === 0) {
        errors.SCRUB_FILL_COLOR = 'Scrubbing fill color is required'
    }

    return errors
}


export function validateRecordAndReplaySettings(settings: Record<string, string>) {
    const errors: Record<string, string> = {}
    if (settings.VIDEO_PIXEL_FORMAT.length === 0) {
        errors.VIDEO_PIXEL_FORMAT = 'Video pixel format is required'
    }
    return errors
}
