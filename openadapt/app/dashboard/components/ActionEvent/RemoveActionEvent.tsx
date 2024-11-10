import { ActionEvent } from '@/types/action-event'
import { Button, Text } from '@mantine/core'
import { modals } from '@mantine/modals'
import { notifications } from '@mantine/notifications'
import React from 'react'

type Props = {
    event: ActionEvent
}

export const RemoveActionEvent = ({ event }: Props) => {
    const openModal = (e: React.MouseEvent<HTMLButtonElement>) => {
        e.stopPropagation()
        modals.openConfirmModal({
            title: 'Please confirm your action',
            children: (
                <Text size="sm">
                    Are you sure you want to delete this action event? This
                    action cannot be undone.
                </Text>
            ),
            labels: { confirm: 'Confirm', cancel: 'Cancel' },
            onCancel: () => {},
            onConfirm: deleteActionEvent,
            confirmProps: { color: 'red' },
        })
    }

    const deleteActionEvent = () => {
        fetch(`/api/action-events/${event.id}`, {
            method: 'DELETE',
        })
            .then((res) => res.json())
            .then((data) => {
                const { message, status } = data
                if (status === 'success') {
                    window.location.reload()
                } else {
                    notifications.show({
                        title: 'Error',
                        message,
                        color: 'red',
                    })
                }
            })
    }
    if (event.isComputed || !event.isOriginal) return null
    return (
        <Button variant="filled" color="red" onClick={openModal}>
            Remove action event
        </Button>
    )
}
