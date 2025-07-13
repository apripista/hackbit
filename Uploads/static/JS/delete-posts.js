function deletePost(postId) {
    if (confirm('Are you sure you want to delete this post?')) {
        fetch(`/delete_post/${postId}`, {
            method: 'POST',
            credentials: 'include'
        })
        .then(response => {
            if (response.ok) {
                location.reload();
            } else {
                console.error('Error deleting post');
            }
        });
    }
}