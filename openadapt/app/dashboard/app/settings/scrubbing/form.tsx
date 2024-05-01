'use client';

import { Button, Checkbox, Flex, Grid, TextInput } from '@mantine/core';
import { useForm } from '@mantine/form';
import { notifications } from '@mantine/notifications';
import React, { useEffect } from 'react'
import { saveSettings, validateScrubbingSettings } from '../utils';

type Props = {
    settings: Record<string, string>,
}

export const Form = ({
    settings,
}: Props) => {
    const form = useForm({
        initialValues: JSON.parse(JSON.stringify(settings)),
        validate: (values) => {
            return validateScrubbingSettings(values);
        },
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
                    <Checkbox label="Scrubbing Enabled" {...form.getInputProps('SCRUB_ENABLED')} checked={form.values.SCRUB_ENABLED} />
                </Grid.Col>
                <Grid.Col span={6}>
                    <TextInput label="Scrubbing character" placeholder="Scrubbing character" {...form.getInputProps('SCRUB_CHAR')} />
                </Grid.Col>
                <Grid.Col span={6}>
                    <TextInput label="Scrubbing language" placeholder="Scrubbing language" {...form.getInputProps('SCRUB_LANGUAGE')} />
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
