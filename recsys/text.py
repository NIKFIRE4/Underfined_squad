"""Текстовые векторы лотов: TF-IDF по леммам (слова и биграммы) → TruncatedSVD → L2-нормировка.

Модель обучается без меток (только тексты), поэтому её можно строить на всех лотах без утечки.
"""
import joblib
import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer


class TextModel:
    def __init__(self, dim=256, max_features=200_000, min_df=3, max_df=0.2, random_state=0):
        self.tfidf = TfidfVectorizer(ngram_range=(1, 2), min_df=min_df, max_df=max_df, max_features=max_features,
                                     sublinear_tf=True, dtype=np.float32, token_pattern=r'(?u)\b[\w-]{2,}\b')
        self.svd = TruncatedSVD(n_components=dim, algorithm='randomized', n_iter=4, random_state=random_state)

    @staticmethod
    def _norm(x):
        x = x.astype(np.float32)
        n = np.linalg.norm(x, axis=1, keepdims=True)
        return x / np.maximum(n, 1e-8)

    def fit_transform(self, docs):
        return self._norm(self.svd.fit_transform(self.tfidf.fit_transform(docs)))

    def transform(self, docs):
        return self._norm(self.svd.transform(self.tfidf.transform(docs)))

    def save(self, path):
        joblib.dump({'tfidf': self.tfidf, 'svd': self.svd}, path)

    @classmethod
    def load(cls, path):
        obj = cls.__new__(cls)
        d = joblib.load(path)
        obj.tfidf, obj.svd = d['tfidf'], d['svd']
        return obj
