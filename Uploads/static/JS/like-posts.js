    function likePost(postId) {
        fetch(`/like_post/${postId}`, {
            method: 'POST',
            credentials: 'same-origin', // include cookies in the request
        })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'liked') {
                // Update the like count in the UI
                const likeCountElement = document.getElementById(`like-count-${postId}`);
                likeCountElement.textContent = data.num_likes;
            } else if (data.status === 'disliked') {
                // Update the like count in the UI
                const likeCountElement = document.getElementById(`like-count-${postId}`);
                likeCountElement.textContent = data.num_likes;
            } else {
                alert(data.message);
            }
        })
        .catch(error => {
            console.error('Error:', error);
        });
    }