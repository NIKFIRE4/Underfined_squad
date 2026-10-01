-- Новые контрагенты (нет в выгрузке «Поставщики_24-25.csv»). Порядок создания важен: внешние ключи.

CREATE TABLE new_cp_sources (
    key text PRIMARY KEY,
    name text NOT NULL,
    url text,
    data_date date
);

CREATE TABLE new_cp_companies (
    inn varchar(12) PRIMARY KEY,
    ogrn varchar(15),
    kind varchar(2) NOT NULL,
    name text,
    name_short text,
    region char(2) NOT NULL,
    city text,
    is_msp boolean NOT NULL,
    msp_category smallint,
    employees integer,
    msp_included_date date,
    okved_main text,
    okved_main_name text,
    okved_add text,
    msp_products text,
    msp_product_names text,
    n_licenses integer,
    license_names text,
    okpd2_groups text NOT NULL,
    okpd2_classes text NOT NULL,
    n_okpd2_groups integer NOT NULL,
    match_evidence text NOT NULL,
    role text NOT NULL,
    role_evidence text,
    role_confidence text NOT NULL,
    tier char(1) NOT NULL,
    tier_label text NOT NULL,
    pool_priority real NOT NULL,
    egrul_check_needed boolean NOT NULL,
    contracts_count integer NOT NULL,
    contracts_customers integer NOT NULL,
    contracts_sum_rub numeric(18,2),
    contracts_first_date date,
    contracts_last_date date,
    contracts_spb_lo integer NOT NULL,
    contracts_customer_regions text,
    contracts_subjects_sample text,
    sources text NOT NULL,
    msp_source text,
    msp_source_date date,
    contracts_source text,
    registry_sources text,
    registry_source_date date,
    retrieved_at date NOT NULL
);
COMMENT ON COLUMN new_cp_companies.inn IS 'ИНН (10 знаков — ЮЛ, 12 — ИП)';
COMMENT ON COLUMN new_cp_companies.ogrn IS 'ОГРН / ОГРНИП (реестр МСП)';
COMMENT ON COLUMN new_cp_companies.kind IS 'UL — юрлицо, IP — индивидуальный предприниматель';
COMMENT ON COLUMN new_cp_companies.name IS 'Полное наименование / ФИО ИП';
COMMENT ON COLUMN new_cp_companies.name_short IS 'Краткое наименование';
COMMENT ON COLUMN new_cp_companies.region IS 'Код региона (реестр МСП; для остальных — по первым цифрам ИНН)';
COMMENT ON COLUMN new_cp_companies.city IS 'Населённый пункт (реестр МСП)';
COMMENT ON COLUMN new_cp_companies.is_msp IS 'Есть в Едином реестре МСП';
COMMENT ON COLUMN new_cp_companies.msp_category IS 'Категория МСП: 1 — микро, 2 — малое, 3 — среднее';
COMMENT ON COLUMN new_cp_companies.employees IS 'Среднесписочная численность (реестр МСП, если указана)';
COMMENT ON COLUMN new_cp_companies.msp_included_date IS 'Дата включения в реестр МСП';
COMMENT ON COLUMN new_cp_companies.okved_main IS 'Основной ОКВЭД';
COMMENT ON COLUMN new_cp_companies.okved_main_name IS 'Наименование основного ОКВЭД';
COMMENT ON COLUMN new_cp_companies.okved_add IS 'Дополнительные ОКВЭД через «;»';
COMMENT ON COLUMN new_cp_companies.msp_products IS 'Коды производимой продукции из реестра МСП через «;»';
COMMENT ON COLUMN new_cp_companies.msp_product_names IS 'Наименования продукции из реестра МСП';
COMMENT ON COLUMN new_cp_companies.n_licenses IS 'Число лицензий в реестре МСП';
COMMENT ON COLUMN new_cp_companies.license_names IS 'Виды лицензируемой деятельности';
COMMENT ON COLUMN new_cp_companies.okpd2_groups IS 'Группы ОКПД2 (XX.XX), которые компания может закрыть, через «;» (до 8)';
COMMENT ON COLUMN new_cp_companies.okpd2_classes IS 'Классы ОКПД2 (XX) через «;»';
COMMENT ON COLUMN new_cp_companies.n_okpd2_groups IS 'Сколько всего групп нашлось до отсечения по 8';
COMMENT ON COLUMN new_cp_companies.match_evidence IS 'Доказательства соответствия (ОКВЭД, контракты, реестры)';
COMMENT ON COLUMN new_cp_companies.role IS 'Роль: производитель / дистрибьютор / поставщик-исполнитель';
COMMENT ON COLUMN new_cp_companies.role_evidence IS 'На чём основана роль';
COMMENT ON COLUMN new_cp_companies.role_confidence IS 'Уверенность роли: высокая / средняя / низкая';
COMMENT ON COLUMN new_cp_companies.tier IS 'Уровень надёжности: A, B, C (см. README)';
COMMENT ON COLUMN new_cp_companies.tier_label IS 'Расшифровка уровня';
COMMENT ON COLUMN new_cp_companies.pool_priority IS 'Приоритет компании в пуле (больше — выше)';
COMMENT ON COLUMN new_cp_companies.egrul_check_needed IS 'Статус ЮЛ не подтверждён реестром МСП — проверить в ЕГРЮЛ перед показом';
COMMENT ON COLUMN new_cp_companies.contracts_count IS 'Контрактов на Портале поставщиков в профильных группах';
COMMENT ON COLUMN new_cp_companies.contracts_customers IS 'Разных заказчиков в этих контрактах';
COMMENT ON COLUMN new_cp_companies.contracts_sum_rub IS 'Сумма этих контрактов, ₽';
COMMENT ON COLUMN new_cp_companies.contracts_first_date IS 'Дата первого контракта';
COMMENT ON COLUMN new_cp_companies.contracts_last_date IS 'Дата последнего контракта';
COMMENT ON COLUMN new_cp_companies.contracts_spb_lo IS 'Из них с заказчиками СПб/ЛО';
COMMENT ON COLUMN new_cp_companies.contracts_customer_regions IS 'Регионы заказчиков (до 5)';
COMMENT ON COLUMN new_cp_companies.contracts_subjects_sample IS 'Примеры предметов контрактов';
COMMENT ON COLUMN new_cp_companies.sources IS 'Ключи источников через «;»: msp, mos, registry (таблица new_cp_sources)';
COMMENT ON COLUMN new_cp_companies.msp_source IS 'Источник полей реестра МСП';
COMMENT ON COLUMN new_cp_companies.msp_source_date IS 'Дата снимка реестра МСП';
COMMENT ON COLUMN new_cp_companies.contracts_source IS 'Источник контрактов';
COMMENT ON COLUMN new_cp_companies.registry_sources IS 'Реестры производителей/лицензий, где найдена компания';
COMMENT ON COLUMN new_cp_companies.registry_source_date IS 'Дата выгрузки реестров';
COMMENT ON COLUMN new_cp_companies.retrieved_at IS 'Дата сборки датасета';

CREATE TABLE new_cp_okpd2 (
    okpd2_group varchar(5) NOT NULL,
    inn varchar(12) NOT NULL REFERENCES new_cp_companies(inn),
    tier char(1) NOT NULL,
    priority real NOT NULL,
    evidence text NOT NULL,
    PRIMARY KEY (okpd2_group, inn)
);
COMMENT ON COLUMN new_cp_okpd2.okpd2_group IS 'Группа ОКПД2 XX.XX';
COMMENT ON COLUMN new_cp_okpd2.inn IS 'ИНН компании';
COMMENT ON COLUMN new_cp_okpd2.tier IS 'Уровень компании A/B/C — первый ключ сортировки';
COMMENT ON COLUMN new_cp_okpd2.priority IS 'Приоритет пары компания × группа — второй ключ сортировки';
COMMENT ON COLUMN new_cp_okpd2.evidence IS 'Почему компания подходит под эту группу';

CREATE TABLE new_cp_contracts (
    supplier_inn varchar(12) NOT NULL REFERENCES new_cp_companies(inn),
    contract_id text NOT NULL,
    okpd2_group varchar(5) NOT NULL,
    okpd2_code text,
    register_number text,
    contract_date date,
    rub_sum numeric(18,2),
    federal_law text,
    customer_inn text,
    customer_name text,
    customer_region text,
    subject text,
    source text NOT NULL,
    PRIMARY KEY (contract_id, supplier_inn, okpd2_group)
);
COMMENT ON COLUMN new_cp_contracts.supplier_inn IS 'ИНН поставщика';
COMMENT ON COLUMN new_cp_contracts.contract_id IS 'ID контракта на Портале поставщиков';
COMMENT ON COLUMN new_cp_contracts.okpd2_group IS 'Группа ОКПД2 позиции контракта';
COMMENT ON COLUMN new_cp_contracts.okpd2_code IS 'Код ОКПД2 позиции';
COMMENT ON COLUMN new_cp_contracts.register_number IS 'Реестровый номер контракта (ЕИС), если есть';
COMMENT ON COLUMN new_cp_contracts.contract_date IS 'Дата заключения';
COMMENT ON COLUMN new_cp_contracts.rub_sum IS 'Сумма контракта, ₽';
COMMENT ON COLUMN new_cp_contracts.federal_law IS '44-ФЗ / 223-ФЗ';
COMMENT ON COLUMN new_cp_contracts.customer_inn IS 'ИНН заказчика';
COMMENT ON COLUMN new_cp_contracts.customer_name IS 'Заказчик';
COMMENT ON COLUMN new_cp_contracts.customer_region IS 'Регион заказчика';
COMMENT ON COLUMN new_cp_contracts.subject IS 'Предмет контракта';
COMMENT ON COLUMN new_cp_contracts.source IS 'Ключ источника (mos)';

CREATE INDEX ix_new_cp_okpd2_search ON new_cp_okpd2 (okpd2_group, tier, priority DESC);
CREATE INDEX ix_new_cp_companies_region ON new_cp_companies (region);
CREATE INDEX ix_new_cp_contracts_supplier ON new_cp_contracts (supplier_inn);
