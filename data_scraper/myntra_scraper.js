(async () => {

    // ============================================================
    // CONFIG
    // ============================================================

    const sleep = ms =>
        new Promise(resolve => setTimeout(resolve, ms));

    const randomDelay = () =>
        1500 + Math.floor(Math.random() * 2000);

    const ROWS = 50;

    let page = 1;

    let paginationContext = null;

    const allProducts = [];


    // ============================================================
    // FETCH ONE PAGE
    // ============================================================

    async function fetchPage(page, paginationContext) {

        const offset =
            page === 1
                ? 0
                : ((page - 1) * ROWS) - 1;


        const url =
            `/gateway/v4/search/columbia` +
            `?rawQuery=columbia` +
            `&rows=${ROWS}` +
            `&o=${offset}` +
            `&plaEnabled=true` +
            `&xdEnabled=false` +
            `&isFacet=true` +
            `&p=${page}` +
            `&pincode=500038`;


        console.log(
            `Fetching Page ${page} | Offset ${offset}`
        );


        // Build headers
        const headers = {
            "accept": "application/json"
        };


        // Send previous page's pagination context
        if (paginationContext) {

            headers["pagination-context"] =
                paginationContext;

            console.log(
                `Sending pagination-context for Page ${page}`
            );

        }


        const response = await fetch(
            url,
            {
                method: "GET",

                credentials: "same-origin",

                headers
            }
        );


        if (!response.ok) {

            throw new Error(
                `HTTP ${response.status} on Page ${page}`
            );

        }


        // Read pagination context BEFORE parsing JSON
        const nextPaginationContext =
            response.headers.get(
                "pagination-context"
            );


        const data =
            await response.json();


        return {

            data,

            paginationContext:
                nextPaginationContext

        };

    }


    // ============================================================
    // MAIN SCRAPER
    // ============================================================

    while (true) {

        let result;


        try {

            result =
                await fetchPage(
                    page,
                    paginationContext
                );

        } catch (error) {

            console.error(
                `FAILED ON PAGE ${page}`,
                error
            );


            console.log(
                "Scraping stopped."
            );


            console.log(
                `Raw products saved: ${allProducts.length}`
            );


            console.log(
                "Your collected data is still available at:"
            );


            console.log(
                "window.__MYNTRA_PRODUCTS__"
            );


            break;

        }


        const data =
            result.data;


        // Update pagination context
        if (result.paginationContext) {

            paginationContext =
                result.paginationContext;


            console.log(
                `✓ Received pagination-context`
            );

        } else {

            console.warn(
                `⚠ No pagination-context returned on Page ${page}`
            );

        }


        // ========================================================
        // FIND PRODUCTS
        // ========================================================

        const products =

            data?.products ||

            data?.results?.products ||

            data?.searchData?.results?.products ||

            [];


        console.log(
            `Page ${page} returned ${products.length} products`
        );


        // ========================================================
        // NORMALIZE PRODUCTS
        // ========================================================

        const scrapedAt =
            new Date().toISOString();


        for (const product of products) {


            const productId =
                String(
                    product.productId ||
                    product.id ||
                    ""
                );


            if (!productId) {

                continue;

            }


            allProducts.push({

                product_id:
                    productId,


                source:
                    "myntra",


                brand:
                    product.brand ||
                    product.brandName ||
                    "Columbia",


                sku:
                    "",


                name:
                    product.productName ||
                    product.name ||
                    "",


                price:
                    String(

                        product.discountedPrice ??

                        product.price ??

                        product.mrp ??

                        ""

                    ),


                url:

                    product.landingPageUrl

                        ? `https://www.myntra.com/${
                            product.landingPageUrl
                                .replace(/^\/+/, "")
                          }`

                        : "",


                image_url:

                    product.searchImage ||

                    product.image ||

                    "",


                available:
                    null,


                scraped_at:
                    scrapedAt

            });

        }


        // ========================================================
        // CREATE LIVE DEDUPLICATED BACKUP
        // ========================================================

        const uniqueMap =
            new Map();


        for (const product of allProducts) {

            if (
                !uniqueMap.has(
                    product.product_id
                )
            ) {

                uniqueMap.set(
                    product.product_id,
                    product
                );

            }

        }


        const uniqueProducts =
            [...uniqueMap.values()];


        // SAVE BACKUP TO WINDOW
        // This survives even if a later fetch fails.

        window.__MYNTRA_PRODUCTS__ =
            uniqueProducts;


        window.__MYNTRA_RAW_PRODUCTS__ =
            allProducts;


        window.__MYNTRA_LAST_PAGE__ =
            page;


        window.__MYNTRA_PAGINATION_CONTEXT__ =
            paginationContext;


        // ========================================================
        // PROGRESS
        // ========================================================

        console.log(
            "--------------------------------"
        );


        console.log(
            `Page: ${page}`
        );


        console.log(
            `Raw collected: ${allProducts.length}`
        );


        console.log(
            `Unique products: ${uniqueProducts.length}`
        );


        console.log(
            `Duplicates: ${
                allProducts.length -
                uniqueProducts.length
            }`
        );


        console.log(
            `API totalCount: ${data.totalCount}`
        );


        console.log(
            `hasNextPage: ${data.hasNextPage}`
        );


        console.log(
            "--------------------------------"
        );


        // ========================================================
        // FINISHED?
        // ========================================================

        if (
            data.hasNextPage === false
        ) {

            console.log(
                "✓ Myntra reports no more pages."
            );

            break;

        }


        // ========================================================
        // SAFETY LIMIT
        // ========================================================

        if (page >= 100) {

            console.warn(
                "Safety limit of 100 pages reached."
            );

            break;

        }


        // ========================================================
        // NEXT PAGE
        // ========================================================

        page++;


        const delay =
            randomDelay();


        console.log(
            `Waiting ${delay}ms before Page ${page}...`
        );


        await sleep(
            delay
        );

    }


    // ============================================================
    // FINAL DEDUPLICATION
    // ============================================================

    const finalMap =
        new Map();


    for (
        const product
        of allProducts
    ) {

        if (
            product.product_id &&
            !finalMap.has(
                product.product_id
            )
        ) {

            finalMap.set(
                product.product_id,
                product
            );

        }

    }


    const finalProducts =
        [...finalMap.values()];


    // Final backup

    window.__MYNTRA_PRODUCTS__ =
        finalProducts;


    // ============================================================
    // FINAL STATS
    // ============================================================

    console.log(
        "========================================"
    );


    console.log(
        "MYNTRA SCRAPING FINISHED"
    );


    console.log(
        `Last Page: ${page}`
    );


    console.log(
        `Raw Listings: ${allProducts.length}`
    );


    console.log(
        `Unique Products: ${finalProducts.length}`
    );


    console.log(
        `Duplicates Removed: ${
            allProducts.length -
            finalProducts.length
        }`
    );


    console.log(
        "Backup:"
    );


    console.log(
        "window.__MYNTRA_PRODUCTS__"
    );


    console.log(
        "========================================"
    );


    // ============================================================
    // DOWNLOAD FUNCTION
    // ============================================================

    // Instead of automatically downloading/navigating,
    // save a function you can manually call.
    //
    // Run:
    //
    // downloadMyntraJSON()
    //
    // after scraping finishes.


    window.downloadMyntraJSON =
        function () {


            const products =
                window.__MYNTRA_PRODUCTS__ ||
                [];


            const json =
                JSON.stringify(
                    products,
                    null,
                    2
                );


            const blob =
                new Blob(
                    [json],
                    {
                        type:
                            "application/json"
                    }
                );


            const blobUrl =
                URL.createObjectURL(
                    blob
                );


            const a =
                document.createElement(
                    "a"
                );


            a.href =
                blobUrl;


            a.download =
                `myntra_columbia_products_${products.length}.json`;


            a.style.display =
                "none";


            document.body.appendChild(
                a
            );


            a.click();


            a.remove();


            setTimeout(
                () => {

                    URL.revokeObjectURL(
                        blobUrl
                    );

                },

                5000

            );


            console.log(
                `Download triggered: ${products.length} products`
            );

        };


    console.log(
        "To download JSON, run:"
    );


    console.log(
        "downloadMyntraJSON()"
    );

})();