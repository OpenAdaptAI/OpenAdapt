import { get } from '@/api';
import moment from 'moment';


export const timeStampToDateString = (timeStamp: number) => {
    if (!timeStamp) {
        return 'N/A';
    }
    return moment.unix(timeStamp).format('DD/MM/YYYY HH:mm:ss');
}


export const getSettings = (category: string = 'general'): Promise<Record<string, any>> => {
    return get(`/api/settings?category=${category}`, {
        cache: 'no-store',
    })
}
