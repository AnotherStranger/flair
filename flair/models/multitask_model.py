import logging
from pathlib import Path
from typing import Union, List, Dict, Tuple, Optional
import random

import torch.nn
from torch.utils.data.dataset import Dataset

import flair.nn
from flair.data import Sentence, Dictionary
from flair.datasets import SentenceDataset, DataLoader
from flair.training_utils import Result

log = logging.getLogger("flair")


class MultitaskModel(flair.nn.Model):
    """
    Multitask Model class which acts as wrapper for creating custom multitask models.
    Takes different tasks as input, parameter sharing is done by objects in flair,
    i.e. creating a Embedding Layer and passing it to two different Models, will
    result in a hard parameter-shared embedding layer. The abstract class takes care
    of calling the correct forward propagation and loss function of the respective
    model.
    """

    def __init__(self, models: Dict):
        """
        :param models: Key (Task ID) - Value (flair.nn.Model) Pairs to stack model
        """
        super(MultitaskModel, self).__init__()

        self.tasks = list()
        label_types = dict()
        for task_id, model in models.items():
            self.__setattr__(task_id, model)
            self.tasks.append(task_id)
            label_types[task_id] = model.label_type
        self._label_type = label_types
        self.to(flair.device)

    def forward_loss(self, sentences: Union[List[Sentence], Sentence]) -> torch.Tensor:
        """
        Abstract forward loss implementation of flair.nn.Model's interface.
        Calls the respective forward loss of each model.
        :param sentences: batch of sentences
        :return: loss
        """
        batch_split = self.split_batch_to_task_ids(sentences)
        loss = 0
        for model, split in batch_split.items():
            task_loss, task_count = self.__getattr__(model).forward_loss([sentences[i] for i in split])
            loss += task_loss / task_count

        return loss

    @staticmethod
    def split_batch_to_task_ids(sentences: Union[List[Sentence], Sentence]) -> Dict:
        """
        Splits a batch of sentences to its respective model. If single sentence is assigned to several tasks
        (i.e. same corpus but different tasks), then the model assignment for this batch is randomly choosen.
        :param sentences: batch of sentences
        :return: Key-value pairs as (task_id, list of sentences ids in batch)
        """
        sent_idx_to_model = {}
        for sentence_id, sentence in enumerate(sentences):
            task = random.choice(sentence.multitask_annotations.get("multitask_assignments"))
            if not task.task_id in sent_idx_to_model:
                sent_idx_to_model[task.task_id] = [sentence_id]
            elif task.task_id in sent_idx_to_model:
                sent_idx_to_model[task.task_id].append(sentence_id)

        return sent_idx_to_model

    def evaluate(
            self,
            data_points: Union[List[Sentence], Dataset],
            gold_label_type: str,
            out_path: Union[str, Path] = None,
            embedding_storage_mode: str = "none",
            mini_batch_size: int = 32,
            num_workers: int = 8,
            main_evaluation_metric: Tuple[str, str] = ("micro avg", "f1-score"),
            exclude_labels: List[str] = [],
            gold_label_dictionary: Optional[Dictionary] = None,
    ) -> Result:
        """
        :param sentences: batch of sentences
        :param embeddings_storage_mode: One of 'none' (all embeddings are deleted and freshly recomputed),
            'cpu' (embeddings are stored on CPU) or 'gpu' (embeddings are stored on GPU)
        :param mini_batch_size: size of batches
        :param num_workers: number of workers for DataLoader class
        :return: Tuple of Result object and loss value (float)
        """

        batch_split = self.split_batch_to_task_ids(data_points)

        # Evaluate each split on its respective model
        results = []
        for task, split in batch_split.items():
            task_result = self.__getattr__(task).evaluate(data_points=[data_points[i] for i in split],
                                                   gold_label_type=gold_label_type[task],
                                                   out_path=out_path,
                                                   embedding_storage_mode=embedding_storage_mode,
                                                   mini_batch_size=mini_batch_size,
                                                   num_workers=num_workers,
                                                   main_evaluation_metric=main_evaluation_metric,
                                                   exclude_labels=exclude_labels,
                                                   gold_label_dictionary=gold_label_dictionary)
            results.append(task_result)
            results.append(self.__getattr__(task).result)
            # Since our Task Model's do not keep track when evaluate is over (they just get a batch of sentences)
            # we need to reset the evaluation metrics after each batch.
            self.__getattr__(task)._reset_eval_metrics()

        result = MultitaskResult(results)

        return result

    def _get_state_dict(self):
        """
        Returns the state dict of the multitask model which has multiple models underneath.
        :return model_state: model state for the multitask model
        """
        model_state = {}

        for task in self.tasks:
            model_state[task] = {"state_dict": self.__getattr__(task)._get_state_dict(),
                                 "class": self.__getattr__(task).__class__}

        return model_state

    @staticmethod
    def _init_model_with_state_dict(state):
        """
        Initializes the model based on given state dict.
        """
        models = {}

        for task, task_state in state.items():
            models[task] = task_state["class"]._init_model_with_state_dict(task_state["state_dict"])

        model = MultitaskModel(models=models)
        return model

    @property
    def label_type(self):
        return self._label_type