export const timeStampToDateString = (timeStamp: number) => {
    if (!timeStamp) {
        return 'N/A';
    }
    const date = new Date(timeStamp * 1000);
    const dateString = `${
        date.getDate().toString().padStart(2, '0')
    }/${(date.getMonth() + 1).toString().padStart(2, '0')}/${date.getFullYear()} ${
        date.getHours()
    }:${date.getMinutes()}:${date.getSeconds()}`;
    return dateString;
}
