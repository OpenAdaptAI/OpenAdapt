"""
Implements a ReplayStrategy mixin for getting text from images via OCR.

Usage:

    class MyReplayStrategy(OCRReplayStrategyMixin):
        ...
"""

import itertools

from loguru import logger
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
from sklearn.cluster import DBSCAN
import mss.base
import numpy as np
import pandas as pd

from puterbot.models import Recording
from puterbot.strategies.base import BaseReplayStrategy


# TODO: use group into sections via layout analysis; see:
# github.com/RapidAI/RapidOCR/blob/main/python/rapid_structure/docs/README_Layout.md


class OCRReplayStrategyMixin(BaseReplayStrategy):
    def __init__(
        self,
        recording: Recording,
    ):
        super().__init__(recording)

        # github.com/RapidAI/RapidOCR/blob/main/python/README.md
        self.ocr = RapidOCR()

    def get_text(
        self,
        screenshot: mss.base.ScreenShot
    ):
        image = Image.frombytes(
            "RGB", screenshot.size, screenshot.bgra, "raw", "BGRX"
        )
        arr = np.array(image)
        result, elapse = self.ocr(arr)
        #det_elapse, cls_elapse, rec_elapse = elapse
        #all_elapse = det_elapse + cls_elapse + rec_elapse
        logger.debug(f"{result=}")
        logger.debug(f"{elapse=}")
        df = get_df(result)
        text = convert_dataframe_to_string(df)
        return text


def unnest(df, explode, axis, suffixes=None):
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


def get_df(result):
	"""
	Convert RapidOCR result to DataFrame.

	Args:
		result: list of [coordinates, text, confidence], where
			coordinates is itself a list of:
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
	logger.info(f"df=\n{df}")
	return df


def preprocess_text(text):
    return text.strip()


def calculate_centroid(row):
    x = (row["tl_x"] + row["tr_x"] + row["bl_x"] + row["br_x"]) / 4
    y = (row["tl_y"] + row["tr_y"] + row["bl_y"] + row["br_y"]) / 4
    return x, y


def calculate_height(row):
    return abs(row["tl_y"] - row["bl_y"])


def sort_rows(df):
    df["centroid"] = df.apply(calculate_centroid, axis=1)
    df["x"] = df["centroid"].apply(lambda coord: coord[0])
    df["y"] = df["centroid"].apply(lambda coord: coord[1])
    df.sort_values(by=["y", "x"], inplace=True)
    return df


def cluster_lines(df, eps):
    coords = df[["x", "y"]].to_numpy()
    cluster_model = DBSCAN(eps=eps, min_samples=1)
    df["line_cluster"] = cluster_model.fit_predict(coords)
    return df


def cluster_words(df):
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


def concat_text(df):
    df.sort_values(by=["line_cluster", "word_cluster"], inplace=True)
    lines = df.groupby("line_cluster")["text"].apply(lambda x: " ".join(x))
    return "\n".join(lines)


def convert_dataframe_to_string(df):
    df["text"] = df["text"].apply(preprocess_text)
    sorted_df = sort_rows(df)
    df["height"] = df.apply(calculate_height, axis=1)
    eps = df["height"].min()
    line_clustered_df = cluster_lines(sorted_df, eps)
    word_clustered_df = cluster_words(line_clustered_df)
    result = concat_text(word_clustered_df)
    return result
