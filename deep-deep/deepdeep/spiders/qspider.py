# -*- coding: utf-8 -*-

import tqdm
from sklearn.feature_extraction.text import HashingVectorizer
from formasaurus.text import normalize
import scrapy
from scrapy.http import TextResponse

from deepdeep.queues import (
    BalancedPriorityQueue,
    RequestsPriorityQueue,
    FLOAT_PRIORITY_MULTIPLIER
)
from deepdeep.spiders.base import BaseSpider
from deepdeep.utils import (
    get_response_domain,
    set_request_domain,
    MaxScores,
)
from deepdeep.score_pages import response_max_scores
from deepdeep.rl.learner import QLearner
from deepdeep.utils import log_time


def _link_inside_text(link):
    text = link.get('inside_text', '')
    title = link.get('attrs', {}).get('title', '')
    return normalize(text + ' ' + title)


def LinkVectorizer():
    return HashingVectorizer(
        preprocessor=_link_inside_text,
        ngram_range=(1, 2),
        n_features=100*1024,
        binary=True,
        norm='l2',
    )


def score_to_priority(score: float) -> int:
    return int(score * FLOAT_PRIORITY_MULTIPLIER)


class QSpider(BaseSpider):
    name = 'q'
    ALLOWED_ARGUMENTS = {'double'} | BaseSpider.ALLOWED_ARGUMENTS
    custom_settings = {
        'DEPTH_LIMIT': 5,
        # 'SPIDER_MIDDLEWARES': {
        #     'deepdeep.spidermiddlewares.CrawlGraphMiddleware': 400,
        # }
    }
    initial_priority = score_to_priority(5)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.Q = QLearner(
            double_learning=kwargs.get('double', True),
            on_model_changed=self.on_model_changed,
        )
        self.link_vec = LinkVectorizer()
        self.total_reward = 0
        self.model_changes = 0
        self.domain_scores = MaxScores(['score'])

    def on_model_changed(self):
        self.model_changes += 1
        if (self.model_changes % 1) == 0:
            self.recalculate_request_priorities()

    def get_reward(self, response: TextResponse) -> float:
        scores = response_max_scores(response)
        # scores.get('registration', 0.0) +
        return scores.get('password/login recovery', 0.0)
        # return scores.get('login', 0.0)

    def parse(self, response):
        self.increase_response_count()

        if 'link' in response.meta:
            reward = self.get_reward(response)
            self.logger.debug("\nGOT {:0.4f} (expected return was {:0.4f}) {}\n{}".format(
                reward,
                response.request.priority / FLOAT_PRIORITY_MULTIPLIER,
                response.url,
                response.meta['link'].get('inside_text'),
            ))

        if not hasattr(response, 'text'):
            if 'link_vector' in response.meta:
                # learn to avoid non-html responses
                self.Q.add_experience(
                    a_t=response.meta['link_vector'],
                    A_t1=None,
                    r_t1=0
                )
                self.debug_Q()
            return

        domain = get_response_domain(response)
        links = list(self.iter_link_dicts(
            response=response,
            domain=domain,
            deduplicate=False
        ))
        links_matrix = self.link_vec.transform(links) if links else None

        if 'link_vector' in response.meta:
            reward = self.get_reward(response)
            self.total_reward += reward
            self.Q.add_experience(
                a_t=response.meta['link_vector'],
                A_t1=links_matrix,
                r_t1=reward
            )
            self.debug_Q()
            self.domain_scores.update(domain, {'score': reward})

        if links:
            _links = list(self.deduplicate_links(links, indices=True))
            if _links:
                indices, links_to_follow = zip(*_links)
                links_to_follow_matrix = links_matrix[list(indices)]
                scores = self.Q.predict(links_to_follow_matrix)

                for link, v, score in zip(links_to_follow, links_to_follow_matrix, scores):
                    meta = {
                        'link_vector': v,
                        'link': link,  # FIXME: turn it off for production
                        'scheduler_slot': domain,
                    }
                    priority = score_to_priority(score)
                    req = scrapy.Request(link['url'], priority=priority, meta=meta)
                    set_request_domain(req, domain)
                    if score > 0.5:
                        self.logger.debug("PROMISING LINK {:0.4f}: {}\n        {}".format(
                            score, link['url'], link['inside_text']
                        ))
                    yield req

    def get_scheduler_queue(self):
        """
        This method is called by scheduler to create a new queue.
        """
        def new_queue(domain):
            return RequestsPriorityQueue(fifo=True)
        return BalancedPriorityQueue(queue_factory=new_queue, eps=0.2)

    @property
    def scheduler_queue(self) -> BalancedPriorityQueue:
        return self.crawler.engine.slot.scheduler.queue

    def get_queue_for_response(self, response: TextResponse) -> RequestsPriorityQueue:
        """ Return a queue response belongs to """
        slot = response.request.meta.get('scheduler_slot')
        return self.scheduler_queue.get_queue(slot)

    @log_time
    def recalculate_request_priorities(self):
        # TODO: vectorize
        def request_priority(request: scrapy.Request):
            link_vector = request.meta.get('link_vector', None)
            if link_vector is None:
                return request.priority
            score = self.Q.predict_one(link_vector)
            if score > 0.5:
                link = request.meta['link']
                self.logger.debug("UPDATED PROMISING LINK {:0.4f}: {}\n        {}".format(
                    score, link['url'], link['inside_text']
                ))
            return score_to_priority(score)

        for slot in tqdm.tqdm(self.scheduler_queue.get_active_slots()):
            queue = self.scheduler_queue.get_queue(slot)
            queue.update_all_priorities(request_priority)

    def debug_Q(self):
        examples = [
            'forgot password',
            'registration',
            'register',
            'sign up',
            'my account',
            'my little pony',
            'comment',
            'sign in',
            'login',
            'forum',
            'forums',
            'sadhjgrhgsfd',
            'забыли пароль'
        ]
        links = [{'inside_text': e} for e in examples]
        A = self.link_vec.transform(links)
        scores_target = self.Q.predict(A)
        scores_online = self.Q.predict(A, online=True)
        for ex, score1, score2 in zip(examples, scores_target, scores_online):
            print("{:20s} {:0.4f} {:0.4f}".format(ex, score1, score2))

        print("t={}, return={:0.4f}, avg return={:0.4f}, L2 norm: {:0.4f} {:0.4f}".format(
            self.Q.t_,
            self.total_reward,
            self.total_reward / self.Q.t_ if self.Q.t_ else 0,
            self.Q.coef_norm(online=True),
            self.Q.coef_norm()
        ))

        scores_sum = sorted(self.domain_scores.sum().items())
        scores_avg = sorted(self.domain_scores.avg().items())
        reward_lines = [
            "{:8.1f}   {:0.4f}   {}".format(tot, avg, k)
            for ((k, tot), (k, avg)) in zip(scores_sum, scores_avg)
        ]
        msg = '\n'.join(reward_lines)
        print(msg)
