import React from 'react'
import Recordings from './recordings/page'

type Route = {
    name: string
    path: string
    component: React.ReactNode
}

export const routes: Route[] = [
    {
        name: 'Recordings',
        path: '/recordings',
        component: <Recordings />,
    },
]
