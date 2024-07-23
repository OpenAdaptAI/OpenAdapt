import { ReplayLog as ReplayLogType } from '@/types/replay';
import { Accordion, Box, Code, Grid, Image, Text } from '@mantine/core';
import React from 'react'

type Props = {
    logs: ReplayLogType[];
}

export const ReplayLogs = ({
    logs
}: Props) => {
  return (
    <Accordion mt={40}>
      {logs.map((log) => (
        <Accordion.Item key={log.id.toString()} value={log.id.toString()}>
          <Accordion.Control>{log.key} - {log.filename}:{log.lineno}</Accordion.Control>
          <Accordion.Panel>
            {log.key === "screenshot" ? (
              <Image src={log.data} alt="screenshot" />
            ) : (log.key === "segmentation" ? (
              <>
                <Image src={log.data.image} alt="segmentation" />
                <Text my={20} fz={24}>Marked Image</Text>
                <Image src={log.data.marked_image} alt="segmentation" />
                <Text my={20} fz={24}>Masked Images</Text>
                <Grid>
                  {log.data.centroids.map((centroid: [number, number], i: number) => {
                    let boundingBox = log.data.bounding_boxes[i];
                    const description = log.data.descriptions[i];
                    const imageShape = log.data.image_shape;

                    return (
                      <Grid.Col span={12}>
                        <svg width="100%" viewBox={`0 0 ${imageShape[0]} ${imageShape[1]}`}>
                          <image href={log.data.image} width="100%" height="100%" />
                          <rect x={boundingBox.left} y={boundingBox.top} width={boundingBox.width} height={boundingBox.height} fill="none" stroke="red" strokeWidth={10} />
                          <circle cx={centroid[0]} cy={centroid[1]} r="1" fill="red" />
                        </svg>
                        <Text>{description}</Text>
                      </Grid.Col>
                    )
                  })}
                </Grid>
              </>
            ): (
              <pre>{JSON.stringify(log.data, null, 4)}</pre>
            ))}
          </Accordion.Panel>
        </Accordion.Item>
      ))}
    </Accordion>
  )
}
