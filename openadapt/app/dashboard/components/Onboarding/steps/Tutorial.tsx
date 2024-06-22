'use client';

import { Box, Text } from '@mantine/core'
import { algora, type AlgoraOutput } from '@algora/sdk';
import React, { useEffect } from 'react'
import Link from 'next/link';

type Bounty = AlgoraOutput['bounty']['list']['items'][number];

function BountyCard(props: { bounty: Bounty }) {
  return (
    <Link
      href={props.bounty.task.url}
      target="_blank"
      rel="noopener"
      className="block group relative h-full rounded-lg border border-gray-400/50 dark:border-indigo-500/50 bg-gradient-to-br from-gray-300/30 via-gray-300/40 to-gray-300/50 dark:from-indigo-600/20 dark:via-indigo-600/30 dark:to-indigo-600/40 md:gap-8 transition-colors hover:border-gray-400 hover:dark:border-indigo-500 hover:bg-gray-300/10 hover:dark:bg-gray-600/5 !no-underline"
    >
      <div className="relative h-full p-4">
        <div className="text-2xl font-bold text-green-500 group-hover:text-green-600 dark:text-green-400 dark:group-hover:text-green-300 transition-colors">
          {props.bounty.reward_formatted}
        </div>
        <div className="mt-0.5 text-sm text-gray-700 dark:text-indigo-200 group-hover:text-gray-800 dark:group-hover:text-indigo-100 transition-colors">
          {props.bounty.task.repo_name}#{props.bounty.task.number}
        </div>
        <div className="mt-3 line-clamp-1 break-words text-lg font-medium leading-tight text-gray-800 dark:text-indigo-50 group-hover:text-gray-900 dark:group-hover:text-white">
          {props.bounty.task.title}
        </div>
      </div>
    </Link>
  );
}

const featuredBountyId = 'clxi7tk210002l20aqlz58ram';

async function getFeaturedBounty() {
  const bounty: Bounty = await algora.bounty.get.query({ id: featuredBountyId });
  return bounty;
}

export const Tutorial = () => {
  const [featuredBounty, setFeaturedBounty] = React.useState<Bounty | null>(null);
  useEffect(() => {
    getFeaturedBounty().then(setFeaturedBounty);
  }, []);
  return (
    <Box>
      <Text my={20} maw={800}>
        Welcome to OpenAdapt! Thank you for joining us on our mission to build open source desktop AI. Your feedback is extremely valuable!
      </Text>
      <Text my={20} maw={800}>
        To start, please watch the demonstration below. Then try it yourself! If you have any issues, please submit a <Link href='https://github.com/OpenAdaptAI/OpenAdapt/issues/new/choose' className='no-underline'>Github Issue.</Link>
      </Text>
      <iframe width="800" height="400" src="https://www.youtube.com/embed/OVERugs50cQ?si=cmi1vY73ADom9EKG" title="YouTube video player" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" referrerPolicy="strict-origin-when-cross-origin" allowFullScreen className='block mx-auto'></iframe>
      <Text my={20} maw={800}>
        If you'd like to contribute directly to our development, please consider the following open Bounties (no development experience required):
        {featuredBounty && <BountyCard bounty={featuredBounty} />}
      </Text>
    </Box>
  )
}
