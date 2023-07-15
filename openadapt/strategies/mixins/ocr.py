"""Implements a ReplayStrategy mixin for getting text from images via OCR.

Uses RapidOCR: github.com/RapidAI/RapidOCR/blob/main/python/README.md

Usage:

    class MyReplayStrategy(OCRReplayStrategyMixin):
        ...
"""

from typing import Union

from loguru import logger
from rapidocr_onnxruntime import RapidOCR
from sklearn.cluster import DBSCAN
import pandas as pd

from openadapt.models import Recording, Screenshot
from openadapt.strategies.base import BaseReplayStrategy

# TODO: group into sections via layout analysis; see:
# github.com/RapidAI/RapidOCR/blob/main/python/rapid_structure/docs/README_Layout.md


class OCRReplayStrategyMixin(BaseReplayStrategy):
    """ReplayStrategy mixin for getting text from images via OCR."""

    def __init__(
        self,
        recording: Recording,
    ) -> None:
        """Initialize the OCRReplayStrategyMixin.

        Args:
            recording (Recording): The recording to replay.
        """
        super().__init__(recording)

        self.ocr = RapidOCR()

    def get_ocr_text(self, screenshot: Screenshot) -> str:
        """Get the OCR text from a screenshot.

        Args:
            screenshot (Screenshot): The screenshot.

        Returns:
            str: The OCR text.
        """
        # TOOD: improve performance
        result, elapse = self.ocr(screenshot.array)
        # det_elapse, cls_elapse, rec_elapse = elapse
        # all_elapse = det_elapse + cls_elapse + rec_elapse
        logger.debug(f"{result=}")
        logger.debug(f"{elapse=}")
        df_text = get_text_df(result)
        text = get_text_from_df(df_text)
        logger.debug(f"{text=}")
        return text


def get_text_df(result: list[list[Union[list[float], str, float]]]) -> pd.DataFrame:
    """Convert RapidOCR result to DataFrame.

    Args:
            result: list of [coordinates, text, confidence]
                    coordinates:
                            [tl_x, tl_y],
                            [tr_x, tr_y],
                            [br_x, br_y],
                            [bl_x, bl_y]

    Returns:
            pd.DataFrame
    """
    coords = [coords for coords, text, confidence in result]
    columns = ["tl", "tr", "bl", "br"]
    df = pd.DataFrame(coords, columns=columns)
    df = unnest(df, df.columns, 0, suffixes=["_x", "_y"])

    texts = [text for coords, text, confidence in result]
    df["text"] = texts

    confidences = [confidence for coords, text, confidence in result]
    df["confidence"] = confidences
    logger.debug(f"df=\n{df}")
    return df


def get_text_from_df(df: pd.DataFrame) -> str:
    """Convert a DataFrame produced by get_text_df into a string.

    Args:
        df (pd.DataFrame): The DataFrame produced by get_text_df.

    Returns:
        str: The converted text.
    """
    df["text"] = df["text"].apply(preprocess_text)
    sorted_df = sort_rows(df)
    df["height"] = df.apply(get_height, axis=1)
    eps = df["height"].min()
    line_clustered_df = cluster_lines(sorted_df, eps)
    word_clustered_df = cluster_words(line_clustered_df)
    result = concat_text(word_clustered_df)
    return result


def unnest(
    df: pd.DataFrame, explode: list, axis: int, suffixes: list = None
) -> pd.DataFrame:
    """Unnest specified columns in a DataFrame by exploding values.

    Args:
        df (pd.DataFrame): DataFrame to unnest.
        explode (list): Columns to unnest.
        axis (int): Axis to unnest along (1 for columns, 0 for rows).
        suffixes (list, optional): Suffixes for unnested column names.

    Returns:
        pd.DataFrame: Unnested DataFrame.

    """
    # https://stackoverflow.com/a/53218939
    if axis == 1:
        df1 = pd.concat([df[x].explode() for x in explode], axis=1)
        return df1.join(df.drop(explode, axis=1), how="left")
    else:
        df1 = pd.concat(
            [
                pd.DataFrame(
                    df[x].tolist(),
                    index=df.index,
                    columns=suffixes,
                ).add_prefix(x)
                for x in explode
            ],
            axis=1,
        )
        return df1.join(
            df.drop(explode, axis=1),
            how="left",
        )


def preprocess_text(text: str) -> str:
    """Preprocess the OCR text.

    Args:
        text (str): The OCR text.

    Returns:
        str: The preprocessed text.
    """
    return text.strip()


def get_centroid(row: pd.Series) -> tuple:
    """Compute the centroid coordinates from the corners of a rectangle.

    Args:
        row (pd.Series): Row containing corner coordinates.

    Returns:
        tuple: Tuple of centroid coordinates (x, y).

    """
    x = (row["tl_x"] + row["tr_x"] + row["bl_x"] + row["br_x"]) / 4
    y = (row["tl_y"] + row["tr_y"] + row["bl_y"] + row["br_y"]) / 4
    return x, y


def get_height(row: pd.Series) -> float:
    """Get the height of a row.

    Args:
        row (pd.Series): The row of the DataFrame.

    Returns:
        float: The height.
    """
    return abs(row["tl_y"] - row["bl_y"])


def sort_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Sort the rows of the DataFrame.

    Args:
        df (pd.DataFrame): The DataFrame to sort.

    Returns:
        pd.DataFrame: The sorted DataFrame.
    """
    df["centroid"] = df.apply(get_centroid, axis=1)
    df["x"] = df["centroid"].apply(lambda coord: coord[0])
    df["y"] = df["centroid"].apply(lambda coord: coord[1])
    df.sort_values(by=["y", "x"], inplace=True)
    return df


def cluster_lines(df: pd.DataFrame, eps: float) -> pd.DataFrame:
    """Cluster lines in the DataFrame.

    Args:
        df (pd.DataFrame): The DataFrame to cluster.
        eps (float): The epsilon value for DBSCAN.

    Returns:
        pd.DataFrame: The DataFrame with line clustering.
    """
    coords = df[["x", "y"]].to_numpy()
    cluster_model = DBSCAN(eps=eps, min_samples=1)
    df["line_cluster"] = cluster_model.fit_predict(coords)
    return df


def cluster_words(df: pd.DataFrame) -> pd.DataFrame:
    """Cluster words in the DataFrame.

    Args:
        df (pd.DataFrame): The DataFrame to cluster.

    Returns:
        pd.DataFrame: The DataFrame with word clustering.
    """
    line_dfs = []
    for line_cluster in df["line_cluster"].unique():
        line_df = df[df["line_cluster"] == line_cluster].copy()

        if len(line_df) > 1:
            coords = line_df[["x", "y"]].to_numpy()
            eps = line_df["height"].min()
            cluster_model = DBSCAN(eps=eps, min_samples=1)
            line_df["word_cluster"] = cluster_model.fit_predict(coords)
        else:
            line_df["word_cluster"] = 0

        line_dfs.append(line_df)
    return pd.concat(line_dfs)


def concat_text(df: pd.DataFrame) -> str:
    """Concatenate text from the DataFrame.

    Args:
        df (pd.DataFrame): The DataFrame with clustered words.

    Returns:
        str: The concatenated text.
    """
    df.sort_values(by=["line_cluster", "word_cluster"], inplace=True)
    lines = df.groupby("line_cluster")["text"].apply(lambda x: " ".join(x))
    return "\n".join(lines)
