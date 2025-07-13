// JavaScript code to hide elements with class 'search-result-message' after 4 minutes
setTimeout(function() {
    var searchResultMessages = document.querySelectorAll('.search-result-message');
    searchResultMessages.forEach(function(message) {
        message.style.display = 'none';
    });
}, 4000);  // 240000 milliseconds = 4 minutes
