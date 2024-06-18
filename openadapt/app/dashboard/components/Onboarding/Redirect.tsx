'use client';

import { get } from "@/api";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";


async function fetchOnboardingStatus() {
    const resp = await get<{ REDIRECT_TO_ONBOARDING: boolean }>('/api/settings?category=general', {
        cache: 'no-store',
    })
    return resp.REDIRECT_TO_ONBOARDING;
}

export const RedirectToOnboarding = () => {
    const [redirect, setRedirect] = useState(false);
    const router = useRouter();

    useEffect(() => {
        fetchOnboardingStatus().then(setRedirect);
    }, []);

    useEffect(() => {
        if (redirect) {
            router.push('/onboarding');
        }
    }, [redirect]);

    return null;
}
