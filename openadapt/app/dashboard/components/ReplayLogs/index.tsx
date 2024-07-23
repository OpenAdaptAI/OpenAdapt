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
                  {log.data.masked_images.map((masked_image: string, i: number) => {
                    let boundingBox = log.data.bounding_boxes[i];
                    let centroid = log.data.centroids[i];
                    const description = log.data.descriptions[i];
                    const shape = log.data.image_shapes[i];

                    boundingBox = {
                      left: boundingBox[0] * 160 / shape[0],
                      top: boundingBox[1] * 100 / shape[1],
                      width: boundingBox[2] * 160 / shape[0],
                      height: boundingBox[3] * 100 / shape[1],
                    }
                    centroid = [
                      centroid[0] * 160 / shape[0],
                      centroid[1] * 100 / shape[1],
                    ]

                    return (
                      <Grid.Col span={6}>
                        <svg width="100%" viewBox="0 0 160 100">
                          <image href={masked_image} width="100%" height="100%" />
                          <rect x={boundingBox.left} y={boundingBox.top} width={boundingBox.width} height={boundingBox.height} fill="none" stroke="red" strokeWidth={0.5} />
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
