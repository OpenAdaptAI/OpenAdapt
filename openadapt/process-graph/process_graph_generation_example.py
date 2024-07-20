import pm4py
import pandas

if __name__ == "__main__":
    log = pandas.read_csv("dataout.csv", sep=",")
    log = pm4py.format_dataframe(
        log,
        case_id="case_id",
        activity_key="activity",
        timestamp_key="timestamp",
        timest_format="%Y-%m-%d %H:%M:%S",
    )

    dfg, start_activities, end_activities = pm4py.discover_dfg(log)
    pm4py.view_dfg(dfg, start_activities, end_activities, format="html")
