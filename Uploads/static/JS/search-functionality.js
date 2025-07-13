        // JavaScript for handling search functionality
        const searchForm = document.getElementById('search-form');
        const searchInput = document.getElementById('search-input');
        const searchCategory = document.getElementById('search-category');

        searchForm.addEventListener('submit', function(event) {
            event.preventDefault();
            const searchQuery = searchInput.value.trim(); // Trim whitespace from the search query
            const category = searchCategory.value; // Get selected search category

            // Redirect to the view_posts route with search query and category as URL parameters
            window.location.href = `/view_posts?q=${encodeURIComponent(searchQuery)}&category=${category}`;
        });