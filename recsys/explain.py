"""Объяснения рекомендаций: причины из вкладов признаков (SHAP LightGBM) и статус по прозрачным правилам."""
import math

import numpy as np
import pandas as pd

from .features import FEATURES, GROUP

MIN_CONTRIB = 0.02  # вклад меньше этого не показываем как причину


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _wins(n):
    return f'{int(n)} {plural(n, "победа", "победы", "побед")}'


def _parts(n):
    return f'{int(n)} {plural(n, "участие", "участия", "участий")}'


def _months(n):
    # Профиль строится на начало месяца лота, поэтому 1 = в прошлом календарном месяце
    n = int(round(n))
    return 'в прошлом месяце' if n <= 1 else f'{n} {plural(n, "месяц", "месяца", "месяцев")} назад'


def _positive(name, r):
    v = r[name]
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if name == 'cust_class_win' and v > 0:
        return f'{_wins(v)} у этого заказчика в классе ОКПД2 лота'
    if name == 'cust_win' and v > 0:
        return f'Работал с этим заказчиком: {_wins(v)}, {_parts(r["cust_part"])}'
    if name == 'cust_m_since_win':
        return f'Последний раз выигрывал у этого заказчика {_months(v)}'
    if name == 'district_class_win' and v > 0:
        return f'{_wins(v)} в этом классе ОКПД2 у заказчиков того же района'
    if name == 'code_win_l6' and v > 0:
        return f'{_wins(v)} с точно таким же кодом ОКПД2'
    if name == 'code_win_l5' and v > 0:
        return f'{_wins(v)} с тем же видом ОКПД2'
    if name == 'code_win_l4' and v > 0:
        return f'{_wins(v)} с той же подгруппой ОКПД2'
    if name == 'code_win_l3' and v > 0:
        return f'{_wins(v)} с той же группой ОКПД2'
    if name == 'code_part_l5' and v > 0:
        return f'{_parts(v)} в закупках того же вида ОКПД2'
    if name == 'code_part_l3' and v > 0:
        return f'{_parts(v)} в закупках той же группы ОКПД2'
    if name == 'code_cover_l5' and v > 0:
        return f'Участвовал в закупках {v:.0%} видов ОКПД2 из этого лота'
    if name == 'code_cover_l3' and v > 0:
        return f'Участвовал в закупках {v:.0%} групп ОКПД2 из этого лота'
    if name == 'code_m_since_win':
        return f'Последняя победа в группе ОКПД2 лота {_months(v)}'
    if name == 'group_share' and v > 0:
        return f'Доля побед в группе ОКПД2 лота: {v:.1%}'
    if name == 'text_cos' and v >= 0.5:
        return f'Предмет закупки похож на его прошлые контракты (сходство {v:.2f})'
    if name == 'price_in_range' and v == 1:
        return 'НМЦК в его обычном ценовом диапазоне'
    if name == 'price_dev' and abs(v) <= 0.7:
        return 'НМЦК близка к его типичным контрактам'
    if name == 's_m_since_win':
        return f'Последняя победа {_months(v)}'
    if name == 's_n_win' and v > 0:
        return f'Опытный поставщик: {_wins(v)} всего'
    if name == 's_win_rate' and v >= 0.5:
        return f'Высокая доля побед: {v:.0%} участий'
    if name == 's_n_customers' and v > 0:
        return f'Работал с {int(v)} {plural(v, "заказчиком", "заказчиками", "заказчиками")}'
    if name == 'platform_fit' and v >= 0.5:
        return f'Обычно работает на этой площадке ({v:.0%} участий)'
    if name == 's_is_spb' and v == 1:
        return 'Зарегистрирован в Санкт-Петербурге'
    if name == 's_n_part' and v > 0:
        return f'{_parts(v)} в закупках всего'
    if name == 'e_okved_main' and v == 1:
        return 'Основной вид деятельности (ОКВЭД) совпадает с группой ОКПД2 лота'
    if name == 'e_okved_any' and v == 1:
        return 'Группа ОКПД2 лота есть среди его видов деятельности (ОКВЭД)'
    if name == 'e_okved_class' and v == 1:
        return 'Класс ОКПД2 лота есть среди его видов деятельности (ОКВЭД)'
    if name == 'e_rev_log' and v > 0:
        return f'Выручка за прошлый год: {_money(v)}'
    if name == 'e_rev_to_price' and v >= math.log(10):
        return f'Выручка за прошлый год в {math.exp(v):,.0f} раз больше НМЦК'.replace(',', ' ')
    if name == 'e_margin' and v > 0:
        return f'Прибыльная компания: рентабельность {v:.0%}'
    if name == 'e_age_years' and v >= 3:
        return f'Работает {int(v)} {plural(int(v), "год", "года", "лет")}'
    if name == 'e_gisp' and v == 1:
        return 'Продукция в реестре промышленной продукции (ГИСП)'
    if name == 'e_software' and v == 1:
        return 'Продукт в реестре российского ПО'
    return None


def _negative(name, r):
    v = r[name]
    nan = v is None or (isinstance(v, float) and math.isnan(v))
    if name == 's_m_since_part':
        return 'Нет истории участий' if nan else f'Последнее участие {_months(v)}'
    if name == 's_m_since_win':
        return 'Ни разу не побеждал' if nan else f'Давно не побеждал: последняя победа {_months(v)}'
    if name == 'price_dev' and not nan:
        k = math.exp(abs(v))
        return f'НМЦК {"выше" if v > 0 else "ниже"} его типичных контрактов примерно в {k:.0f} раз' if k >= 2 else None
    if name == 'price_in_range' and v == 0:
        return 'НМЦК вне его обычного ценового диапазона'
    if name == 'platform_fit' and not nan and v < 0.5:
        return f'Редко работает на этой площадке ({v:.0%} участий)'
    if name == 'code_win_l6' and v == 0:
        return 'Нет побед с точно таким же кодом ОКПД2'
    if name == 'code_win_l5' and v == 0:
        return 'Нет побед с тем же видом ОКПД2'
    if name == 'code_win_l3' and v == 0:
        return 'Нет побед с той же группой ОКПД2'
    if name == 'cust_win' and v == 0:
        return 'Не работал с этим заказчиком'
    if name == 's_win_rate' and not nan and v < 0.5:
        return f'Низкая доля побед: {v:.0%} участий'
    if name == 'text_cos' and not nan and v < 0.5:
        return f'Предмет закупки мало похож на его прошлые контракты (сходство {v:.2f})'
    if name == 'e_okved_any' and v == 0:
        return 'Группы ОКПД2 лота нет среди его видов деятельности (ОКВЭД)'
    if name == 'e_neg_equity' and v == 1:
        return 'Отрицательный капитал по отчётности за прошлый год'
    if name == 'e_margin' and not nan and v < 0:
        return 'Убыток по отчётности за прошлый год'
    if name == 'e_rev_to_price' and not nan and v < 0:
        return 'Выручка за прошлый год меньше НМЦК лота'
    if name == 'e_age_years' and not nan and v < 1:
        return 'Компания моложе года'
    return None


def _money(log_v):
    """log(1+₽) → «12,3 млн ₽»."""
    x = math.expm1(log_v)
    for div, unit in ((1e9, 'млрд'), (1e6, 'млн'), (1e3, 'тыс.')):
        if x >= div:
            return f'{x / div:.1f} {unit} ₽'.replace('.', ',')
    return f'{x:.0f} ₽'


COUNT = {'s_n_win', 's_n_part', 's_n_customers', 'code_win_l6', 'code_win_l5', 'code_win_l4', 'code_win_l3',
         'code_part_l5', 'code_part_l3', 'cust_win', 'cust_part', 'cust_class_win', 'district_class_win',
         's_win_3m', 's_win_6m', 's_part_6m', 'code_win_l5_6m', 'code_win_l3_6m', 'code_win_l3_3m', 'code_part_l3_6m',
         'cust_win_12m'}
SHARE = {'s_win_rate', 's_em_share', 'platform_fit', 'code_cover_l5', 'code_cover_l3', 'group_share',
         'spec_l5', 'spec_l3', 'spec_part_l3', 'rel_code_win_l5', 'rel_code_win_l3', 'rel_code_win_l3_6m', 'rel_cust_win'}
MONTHS = {'s_m_since_win', 's_m_since_part', 'code_m_since_win', 'cust_m_since_win'}


def value_text(name, v):
    """Значение признака для интерфейса: «7», «85%», «3 месяца назад», «нет»."""
    v = None if v is None else float(v)  # numpy.float32 из матрицы признаков
    if v is None or math.isnan(v):
        return 'никогда' if name in MONTHS else 'нет данных'
    if name in COUNT:
        return f'{int(v):,}'.replace(',', ' ')
    if name in SHARE:
        return (f'{v:.1%}' if name == 'group_share' and v < 0.1 else f'{v:.0%}').replace('.', ',')
    if name in MONTHS:
        return _months(v)
    if name in ('s_is_ip', 's_is_spb', 'price_in_range'):
        return 'да' if v == 1 else 'нет'
    if name == 'rel_text':
        return 'лучшее в лоте' if v >= -0.005 else f'на {abs(v):.2f} ниже лучшего'.replace('.', ',')
    if name == 'text_cos':
        return f'{v:.2f}'.replace('.', ',')
    if name == 'price_dev':
        k = math.exp(abs(v))
        return 'как обычно' if k < 1.5 else f'в {k:.0f} раз {"выше" if v > 0 else "ниже"} обычной'
    if name in ('e_okved_main', 'e_okved_any', 'e_okved_class', 'e_gisp', 'e_software', 'e_neg_equity'):
        return 'да' if v == 1 else 'нет'
    if name in ('e_rev_log', 'e_assets_log'):
        return _money(v)
    if name == 'e_rev_to_price':
        k = math.exp(abs(v))
        return 'около НМЦК' if k < 1.5 else f'в {k:,.0f} раз {"больше" if v > 0 else "меньше"} НМЦК'.replace(',', ' ')
    if name == 'e_margin':
        return f'{v:.0%}'
    if name == 'e_age_years':
        r = round(v, 1)
        return f'{int(r)} {plural(int(r), "год", "года", "лет")}' if r == int(r) else f'{r:.1f} года'.replace('.', ',')
    if name == 'e_msp_cat':
        return {1: 'микропредприятие', 2: 'малое', 3: 'среднее'}.get(int(v), f'{v:g}')
    if name in ('e_n_okved', 'e_licenses'):
        return f'{int(v)}'
    return f'{v:g}'


def status(r):
    """Статус контрагента по прозрачному правилу. Возвращает (статус, правило)."""
    if math.isnan(r['code_cover_l3']):  # у лота нет кодов ОКПД2 из истории — категорию сравнить не с чем
        if r['s_n_win'] >= 3 and r['text_cos'] >= 0.5:
            return 'Проверенный', 'Код ОКПД2 не указан: ≥ 3 побед и предмет похож на его прошлые контракты'
        if r['s_n_part'] > 0:
            return 'Активный участник', 'Код ОКПД2 не указан: есть история участия в закупках'
        return 'Новый в пуле', 'Нет истории закупок в данных'
    if r['code_win_l3'] >= 3 and not math.isnan(r['code_m_since_win']) and r['code_m_since_win'] <= 12:
        return 'Проверенный', '≥ 3 побед в группе ОКПД2 лота, последняя — не раньше 12 месяцев назад'
    if r['code_part_l3'] > 0:
        return 'Активный участник', 'Участвовал в закупках группы ОКПД2 лота'
    if r['s_n_part'] > 0:
        return 'Новый в категории', 'Есть история закупок, но не в группе ОКПД2 лота'
    return 'Новый в пуле', 'Нет истории закупок в данных'


def explain(features_df, contrib, cols=FEATURES, n_pos=3, n_neg=1):
    """Причины для каждой строки. contrib — вклады из booster.predict(..., pred_contrib=True), cols — признаки модели."""
    cols = list(cols)
    usable = np.array([GROUP[c] != 'лот' for c in cols])  # контекст лота одинаков для всех кандидатов
    out = []
    recs = features_df[cols].to_dict('records')
    for r, c in zip(recs, contrib[:, :len(cols)]):
        order = np.argsort(-c)
        pos = []
        for j in order:
            if c[j] < MIN_CONTRIB or len(pos) >= n_pos:
                break
            if usable[j]:
                t = _positive(cols[j], r)
                if t and t not in pos:
                    pos.append(t)
        neg = []
        for j in order[::-1]:
            if c[j] > -MIN_CONTRIB or len(neg) >= n_neg:
                break
            if usable[j]:
                t = _negative(cols[j], r)
                if t and t not in neg:
                    neg.append(t)
        st, rule = status(r)
        out.append({'reasons': pos, 'risks': neg, 'status': st, 'status_rule': rule})
    return pd.DataFrame(out, index=features_df.index)
