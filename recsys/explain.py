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
    return None


def status(r):
    """Статус контрагента по прозрачному правилу. Возвращает (статус, правило)."""
    if r['code_win_l3'] >= 3 and not math.isnan(r['code_m_since_win']) and r['code_m_since_win'] <= 12:
        return 'Проверенный', '≥ 3 побед в группе ОКПД2 лота, последняя — не раньше 12 месяцев назад'
    if r['code_part_l3'] > 0:
        return 'Активный участник', 'Участвовал в закупках группы ОКПД2 лота'
    if r['s_n_part'] > 0:
        return 'Новый в категории', 'Есть история закупок, но не в группе ОКПД2 лота'
    return 'Новый в пуле', 'Нет истории закупок в данных'


def explain(features_df, contrib, n_pos=3, n_neg=1):
    """Причины для каждой строки. contrib — вклады из booster.predict(..., pred_contrib=True)."""
    cols = list(FEATURES)
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
