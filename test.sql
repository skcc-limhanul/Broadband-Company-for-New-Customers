SELECT *
FROM subscribers_historical
ORDER BY active_promo DESC
LIMIT 10
;
SELECT *
FROM promotion
WHERE service = '초고속'
    AND service_sub = 'IP'
    AND channel = 'SKT'
    AND promo_start_at <= '2026-05-14'
    AND promo_end_at >= '2026-05-14'
;
SELECT *
FROM (
        SELECT subh.date,
            subh.service,
            subh.service_sub,
            subh.channel,
            subh.subscriber_new AS subscriber,
            promo.promo_id
        FROM subscribers_historical AS subh
            LEFT JOIN promotion AS promo ON subh.date BETWEEN promo.promo_start_at AND promo.promo_end_at
    ) AS tt1
-- WHERE tt1.promo_id IS NULL
LIMIT 50
;


SELECT promo.promo_id,
    promo.promo_title,
    promo.promo_start_at,
    promo.promo_end_at,
    subh.date as promo_date,
    subh.subscriber_new as promo_subscriber,
    promo.service,
    promo.service_sub,
    promo.channel,
    promo.target_customer,
    promo.cross_condition,
    promo.discount_type,
    promo.price_base,
    promo.price_promo,
    promo.total_discount_customer,
    promo.contract_month,
    promo.minimum_subscription_day
FROM promotion AS promo
    LEFT JOIN subscribers_historical AS subh ON promo.service = subh.service
    AND promo.service_sub = subh.service_sub
    AND promo.channel = subh.channel
    AND subh.date BETWEEN promo.promo_start_at AND promo.promo_end_at
WHERE promo.subscription_type = '신규'
    AND promo.promo_start_at >= '2026-04-01'
ORDER BY promo.promo_start_at
LIMIT 500