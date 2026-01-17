import moment from 'moment';


export const timeStampToDateString = (timeStamp: number) => {
    if (!timeStamp) {
        return 'N/A';
    }
    return moment.unix(timeStamp).format('DD/MM/YYYY HH:mm:ss');
}
