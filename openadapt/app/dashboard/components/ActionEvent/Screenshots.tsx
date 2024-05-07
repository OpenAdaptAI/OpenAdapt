import { ActionEvent } from '@/types/action-event'
import { Carousel } from '@mantine/carousel';
import { Image, Modal } from '@mantine/core';
import React, { useState } from 'react'

type Props = {
    events: ActionEvent[];
    isOpen: boolean;
    onClose: () => void;
}

export const Screenshots = ({
    events,
    isOpen,
    onClose,
}: Props) => {
    const aspectRatio = 16 / 9;
    const height = window.innerHeight * 0.8;
    const width = height * aspectRatio;
  return (
    <Modal opened={isOpen} onClose={onClose} centered withCloseButton={false} size="90%" closeOnClickOutside closeOnEscape>
        <Carousel withIndicators height={height} slideSize="100%" slideGap="md" align="start" defaultValue={154}>
            {events.map((event) => (
                event.screenshot &&
                <Image
                    src={event.screenshot}
                    alt="screenshot"
                    width={width}
                    height={height}
                    mx={20}
                    key={event.id}
                />
            ))}
        </Carousel>
    </Modal>
  )
}
