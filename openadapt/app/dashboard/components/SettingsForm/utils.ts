export function validateSettings(settings: Record<string, string>) {
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
