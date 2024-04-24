'use client';

import { Button, Fieldset, Flex, Grid, Stack, TextInput } from '@mantine/core';
import { useForm } from '@mantine/form';
import React, { useEffect } from 'react'
import { saveSettings } from '../utils';

type Props = {
    settings: Record<string, string>,
}

export const Form = ({
    settings,
}: Props) => {
    const form = useForm({
        initialValues: JSON.parse(JSON.stringify(settings)),
    })

    useEffect(() => {
        form.setValues(JSON.parse(JSON.stringify(settings)));
        form.setInitialValues(JSON.parse(JSON.stringify(settings)));
    }, [settings]);

    function resetForm() {
        form.reset();
    }
    return (
        <form onSubmit={form.onSubmit(saveSettings(form))}>
            <Grid>
                <Grid.Col span={6}>
                    <Fieldset legend="PRIVACY">
                        <Stack>
                            <TextInput label="PRIVATE_AI_API_KEY" placeholder="Please enter your Private AI API key" {...form.getInputProps('PRIVATE_AI_API_KEY')} />
                        </Stack>
                    </Fieldset>
                </Grid.Col>
                <Grid.Col span={6}>
                    <Fieldset legend="SEGMENTATION">
                        <Stack>
                            <TextInput label="REPLICATE_API_TOKEN" placeholder="Please enter your Replicate API token" {...form.getInputProps('REPLICATE_API_TOKEN')} />
                        </Stack>
                    </Fieldset>
                </Grid.Col>
                <Grid.Col span={6}>
                    <Fieldset legend="COMPLETIONS">
                        <Stack>
                            <TextInput label="OPENAI_API_KEY" placeholder="Please enter your OpenAI API key" {...form.getInputProps('OPENAI_API_KEY')} />
                            <TextInput label="ANTHROPIC_API_KEY" placeholder="Please enter your Anthropic API key" {...form.getInputProps('ANTHROPIC_API_KEY')} />
                            <TextInput label="GOOGLE_API_KEY" placeholder="Please enter your Google API key" {...form.getInputProps('GOOGLE_API_KEY')} />
                        </Stack>
                    </Fieldset>
                </Grid.Col>
            </Grid>
            <Flex mt={40} columnGap={20}>
                <Button disabled={!form.isDirty()} type="submit">
                    Save settings
                </Button>
                <Button variant="subtle" disabled={!form.isDirty()} onClick={resetForm}>
                    Discard changes
                </Button>
            </Flex>
        </form>
    )
}
